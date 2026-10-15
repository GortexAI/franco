"""Record the README's dashboard GIF (docs/dashboard.gif) from made-up demo data.

The dashboard reads and writes ``<repo>/data``, so this never runs against the
real checkout: it copies the package into a throwaway directory, seeds a demo
database there, serves the dashboard from that copy, and drives it with a
headless browser. Your own data/franco.db, .env and franco.yaml are never read
or touched.

    pip install -e ".[demo]" && playwright install chromium
    python scripts/record_demo.py                 # writes docs/dashboard.gif

Every frame is an explicit screenshot taken after a scripted step, so the GIF
comes out the same on every run regardless of machine speed.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml
from PIL import Image
from playwright.async_api import async_playwright

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from franco.models.company import Company  # noqa: E402
from franco.models.prospect import Prospect  # noqa: E402
from franco.signals import seed_signal_catalog  # noqa: E402
from franco.state import StateManager  # noqa: E402

WIDTH, HEIGHT = 900, 560      # output size, matching the README's layout
VIEW_W, VIEW_H = 1200, 746    # browser window: wide enough for the whole nav
SCALE = 1.5                   # render at 1800px wide and halve it, for crisp text
MOVE_MS = 40                  # per-frame delay while the cursor or page moves

# ── Demo data: invented marketplaces and feed communities, Gortex's two tracks ──
#
# Every company, person and observation below is fictional. The outbox copy is
# the M1/F1 opening email from the Gortex outbound playbook, with the
# {{verified_observation}} merge field filled from the demo signals.

MARKETPLACE, COMMUNITY = "Vertical marketplace", "Feed-based community"

# (name, domain, industry, location, profile signals)
COMPANIES = [
    ("Tradeloft", "tradeloft.co", MARKETPLACE, "Chicago, IL",
     {"HIRING_ROLE": "Search Ranking Engineer", "TECH_STACK": "Elasticsearch",
      "RUNNING_GOOGLE_ADS": 1, "SITE_PAGE_COUNT": 48000}),
    ("Petlane", "petlane.app", MARKETPLACE, "Denver, CO",
     {"HIRING_ROLE": "Head of Monetization", "TECH_STACK": "Algolia",
      "RUNNING_GOOGLE_ADS": 1, "RUNNING_META_ADS": 1}),
    ("Tutorgrove", "tutorgrove.com", MARKETPLACE, "Austin, TX",
     {"HIRING_ROLE": "Ads Product Manager", "TECH_STACK": "Typesense",
      "RUNNING_GOOGLE_ADS": 1}),
    ("Rentvale", "rentvale.co", MARKETPLACE, "Atlanta, GA",
     {"TECH_STACK": "Elasticsearch", "RUNNING_GOOGLE_ADS": 1,
      "SITE_PAGE_COUNT": 21000}),
    ("Fieldhand Jobs", "fieldhandjobs.com", MARKETPLACE, "Columbus, OH",
     {"HIRING_ROLE": "Marketplace Product Lead", "RUNNING_META_ADS": 1}),
    ("Gearloft Exchange", "gearloftexchange.com", MARKETPLACE, "Boulder, CO",
     {"TECH_STACK": "Algolia", "SITE_PAGE_COUNT": 36000}),
    ("Threadwise", "threadwise.dev", COMMUNITY, "Brooklyn, NY",
     {"HIRING_ROLE": "Sponsorships Lead", "TECH_STACK": "Elasticsearch",
      "RUNNING_META_ADS": 1}),
    ("Climbline", "climbline.app", COMMUNITY, "Salt Lake City, UT",
     {"TECH_STACK": "Algolia", "RUNNING_META_ADS": 1, "RUNNING_GOOGLE_ADS": 1}),
    ("Ledger Lounge", "ledgerlounge.com", COMMUNITY, "Charlotte, NC",
     {"HIRING_ROLE": "Head of Partnerships", "RUNNING_GOOGLE_ADS": 1}),
    ("Brushstroke Social", "brushstroke.social", COMMUNITY, "Portland, OR",
     {"TECH_STACK": "Typesense", "SITE_PAGE_COUNT": 12000}),
    ("Kitchen Table Network", "kitchentable.network", COMMUNITY, "Minneapolis, MN", {}),
    ("Trailhead Collective", "trailheadcollective.co", COMMUNITY, "Bend, OR", {}),
]

# (company index, first, last, title)
CONTACTS = [
    (0, "Dana", "Whitfield", "CEO"),
    (1, "Marcus", "Ibe", "Head of Marketplace"),
    (2, "Lena", "Sorensen", "VP Product"),
    (3, "Arjun", "Mehta", "General Manager"),
    (6, "Noor", "Haddad", "Chief Product Officer"),
    (7, "Caleb", "Brandt", "CEO"),
    (8, "Simone", "Achebe", "Head of Revenue"),
]

M1_PITCH = ("I'm building Gortex to help marketplaces introduce relevant sponsored "
            "listings without taking on the entire ranking and monetization "
            "infrastructure project.")
F1_PITCH = ("I'm building Gortex to help feed-based platforms make sponsored content "
            "part of a deliberate discovery experience, rather than a separate "
            "insertion process.")

# (contact index, subject, observation, pitch, close)
OUTBOX = [
    (0, "Sponsored listings at Tradeloft",
     "Tradeloft's seller help center describes a paid Featured Listing upgrade",
     M1_PITCH, "Would a short outline of a one-surface pilot be useful?"),
    (4, "Sponsored posts at Threadwise",
     "Threadwise's advertise page offers sponsored posts in the main developer feed",
     F1_PITCH, "Would a short native-sponsorship outline be useful?"),
    (1, "Sponsored listings at Petlane",
     "Petlane is hiring a Head of Monetization",
     M1_PITCH, "Would a short outline of a one-surface pilot be useful?"),
    (5, "Sponsored posts at Climbline",
     "Climbline's partner page lists gear brands sponsoring the route feed",
     F1_PITCH, "Would a short native-sponsorship outline be useful?"),
]

PROPOSED = {"SERP_RANK", "LIKELY_OWNER", "REGISTRY_VERIFIED"}  # left for the viewer


def email_body(first: str, observation: str, pitch: str, close: str) -> str:
    return (f"Hi {first},\n\nI noticed {observation}.\n\n{pitch}\n\n{close}\n\nHemin")


def demo_config() -> dict:
    cfg = yaml.safe_load((REPO / "franco.yaml").read_text())
    cfg["persona"].update(name="Hemin", company="Gortex", role="Founder",
                          email="hj@gortex.io", linkedin="",
                          tone="direct, specific, never overclaims")
    cfg["product"].update(
        name="Gortex",
        description="Discovery and monetization infrastructure: helps marketplaces "
                    "and feed platforms rank their content and sell relevant "
                    "sponsored visibility through an API.",
        pricing="Paid pilot on one ranked surface",
    )
    cfg["icp"].update(industries=[MARKETPLACE, COMMUNITY],
                      titles=["CEO", "Chief Product Officer", "Head of Marketplace",
                              "Head of Monetization"],
                      company_size="50-500 employees", geography=["United States"],
                      hiring_signals=["Head of Monetization", "Search Ranking Engineer",
                                      "Ads Product Manager"])
    cfg["channels"]["email"]["provider"] = "gmail"
    cfg["channels"]["linkedin"]["enabled"] = False
    return cfg


def make_sandbox() -> Path:
    """A minimal, disposable checkout the dashboard will treat as its project."""
    root = Path(tempfile.mkdtemp(prefix="franco-demo-"))
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for d in ("franco", "prompts", "skills"):
        shutil.copytree(REPO / d, root / d, ignore=ignore)
    (root / "franco.yaml").write_text(yaml.safe_dump(demo_config(), sort_keys=False))
    # Placeholder values so the setup checklist reads as finished; nothing here
    # is a real credential and nothing ever calls out with them.
    (root / ".env").write_text("GMAIL_CLIENT_ID=demo\nGMAIL_CLIENT_SECRET=demo\n"
                               "REOON_API_KEY=demo\n")
    (root / ".venv").mkdir()
    (root / "data").mkdir()
    (root / "data" / "gmail_token.json").write_text("{}")
    (root / "skills" / "product_knowledge.md").write_text("# Gortex\n")
    return root


async def seed(db_path: Path):
    state = StateManager(db_path=str(db_path))
    await state.init_db()
    await seed_signal_catalog(state)
    for sig in await state.get_signal_codes():
        if sig["code"] not in PROPOSED:
            await state.set_signal_status(sig["code"], "confirmed")

    run = await state.start_run("discover", provider="serper",
                                params={"terms": [MARKETPLACE, COMMUNITY]})
    company_ids = []
    for name, domain, industry, location, _ in COMPANIES:
        cid = await state.add_company(Company(
            name=name, domain=domain, website=f"https://{domain}",
            industry=industry, location=location, source="serper"))
        company_ids.append(cid)
        await state.add_observation(
            "FOUND_IN_SERP", company_id=cid, collector="discover", run_id=run,
            value_text=f"{industry.lower()} United States")
    await state.finish_run(run, records=len(COMPANIES), cost_usd=0.0036)

    run = await state.start_run("profile")
    for cid, (_, domain, _, _, signals) in zip(company_ids, COMPANIES):
        if not signals:
            continue  # left unread, so "Not yet read" isn't zero
        rows = []
        for code, value in signals.items():
            row = {"company_id": cid, "signal_code": code, "collector": "profile",
                   "evidence_url": f"https://{domain}/", "confidence": 0.9}
            if isinstance(value, str):
                row["value_text"] = value
            else:
                row["value_num"] = float(value)
            rows.append(row)
        await state.add_observations(rows, run_id=run)
        await state.mark_company_profiled(cid)
    await state.finish_run(run, records=sum(1 for c in COMPANIES if c[4]))

    prospect_ids, emails = [], []
    for ci, first, last, title in CONTACTS:
        name, domain, industry = COMPANIES[ci][:3]
        emails.append(f"{first.lower()}@{domain}")
        prospect_ids.append(await state.add_prospect(Prospect(
            company_id=company_ids[ci], first_name=first, last_name=last, title=title,
            email=emails[-1], email_verified=True, email_status="verified",
            company=name, industry=industry, source="team_page", score=8)))

    send_at = datetime.now().replace(hour=9, minute=15, second=0) + timedelta(days=1)
    for n, (pi, subject, observation, pitch, close) in enumerate(OUTBOX):
        await state.add_outbox_item(
            prospect_id=prospect_ids[pi], to_email=emails[pi], subject=subject,
            body=email_body(CONTACTS[pi][1], observation, pitch, close),
            provider="gmail", send_at=(send_at + timedelta(minutes=7 * n)).isoformat())

    for action in ("prospect", "write_campaign", "handle_replies", "send_outbox",
                   "prospect", "analyze"):
        await state.log_action(action, "main", {})


# ── Recording ──

CURSOR_JS = """
document.addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.id = '__cursor';
  c.innerHTML = '<svg width="18" height="22" viewBox="0 0 18 22"><path d="M1 1 L1 17 ' +
    'L5.5 13 L8.5 20 L11.5 18.7 L8.6 12 L14.5 12 Z" fill="#111" stroke="#fff" ' +
    'stroke-width="1.4" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, {position: 'fixed', left: '-40px', top: '-40px',
    zIndex: 2147483647, pointerEvents: 'none'});
  document.body.appendChild(c);
  document.addEventListener('mousemove', e => {
    c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px';
  }, true);
});
"""


class Recorder:
    def __init__(self, page):
        self.page = page
        self.frames: list[tuple[Image.Image, int]] = []
        self.mouse = (VIEW_W * 0.6, VIEW_H * 0.5)

    async def snap(self, ms: int):
        png = await self.page.screenshot(type="png")
        img = Image.open(io.BytesIO(png)).convert("RGB").resize(
            (WIDTH, HEIGHT), Image.LANCZOS)
        if self.frames and self.frames[-1][0].tobytes() == img.tobytes():
            prev, d = self.frames[-1]
            self.frames[-1] = (prev, d + ms)
        else:
            self.frames.append((img, ms))

    async def hold(self, ms: int):
        await self.page.wait_for_timeout(250)  # let fetches and transitions settle
        await self.snap(ms)

    async def move_to(self, locator, steps: int = 12, dx: float | None = None):
        box = await locator.bounding_box()
        x = box["x"] + (dx if dx is not None else min(box["width"] / 2, 60))
        y = box["y"] + box["height"] / 2
        x0, y0 = self.mouse
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in-out
            await self.page.mouse.move(x0 + (x - x0) * t, y0 + (y - y0) * t)
            await self.snap(MOVE_MS)
        self.mouse = (x, y)

    async def click(self, locator, settle: int = 900):
        await locator.scroll_into_view_if_needed()
        await self.move_to(locator)
        await self.page.mouse.down()
        await self.snap(80)
        await self.page.mouse.up()
        await self.hold(settle)

    async def scroll_to(self, y_target: float | None = None, locator=None, steps: int = 12):
        if locator is not None:
            box = await locator.bounding_box()
            y_target = await self.page.evaluate("window.scrollY") + box["y"] - 90
        y0 = await self.page.evaluate("window.scrollY")
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)
            await self.page.evaluate(f"window.scrollTo(0, {y0 + (y_target - y0) * t})")
            await self.snap(MOVE_MS)


async def storyboard(rec: Recorder, url: str):
    page = rec.page
    await page.goto(url)
    await page.wait_for_selector("#today-queue .queue-item")
    await page.wait_for_selector("#today-activity .activity-item")
    await page.mouse.move(*rec.mouse)
    await rec.hold(2200)

    # Confirm a signal straight from the Today queue
    await rec.click(page.locator("#today-queue button", has_text="Review signals"), 1200)
    confirm = page.locator(".sig-row button", has_text="Confirm").first
    await rec.scroll_to(locator=confirm)
    await rec.click(confirm, 1600)

    # Build a cohort and watch the count update
    await rec.scroll_to(locator=page.locator("#cohort-builder"), steps=16)
    must = page.locator("#cohort-builder .cohort-grid").first
    for label in ("Hiring for a relevant role", "Website platform / tools",
                  "Running Google Ads"):
        await rec.click(must.locator(".cohort-pick", has_text=label), 700)
    await rec.scroll_to(locator=page.locator("#cohort-result"), steps=10)
    await rec.hold(1800)

    # Price a discovery run before spending anything
    await rec.scroll_to(0, steps=10)
    await rec.click(page.locator("nav button", has_text="Discover"), 1000)
    await rec.click(page.locator(".prov-card", has_text="DataForSEO SERP"), 900)
    estimate = page.locator("button", has_text="Estimate cost")
    await rec.scroll_to(locator=estimate, steps=12)
    await rec.click(estimate, 300)
    await page.wait_for_selector("#discover-estimate *")
    await rec.move_to(page.locator("#discover-estimate"), dx=560)
    await rec.hold(1800)

    # Work the outbox one email at a time
    await rec.scroll_to(0, steps=10)
    await rec.click(page.locator("nav button", has_text="Outbox"), 1400)
    await rec.click(page.locator(".desk-actions button", has_text="Approve"), 1400)
    await page.keyboard.press("a")
    await rec.hold(1400)

    # Same design, re-tokenised for dark
    await rec.click(page.locator("#theme-btn"), 1400)
    await rec.click(page.locator("nav button", has_text="Today"), 2400)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_gif(frames: list[tuple[Image.Image, int]], out: Path):
    # Greyscale chrome plus a few status hues fits one shared palette, and no
    # dithering keeps text clean and the file small.
    palette = frames[0][0].quantize(colors=255, method=Image.Quantize.MEDIANCUT)
    imgs = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in frames]
    out.parent.mkdir(parents=True, exist_ok=True)
    imgs[0].save(out, save_all=True, append_images=imgs[1:], loop=0, optimize=True,
                 duration=[d for _, d in frames])


async def record(out: Path, keep: bool):
    sandbox = make_sandbox()
    server = None
    try:
        await seed(sandbox / "data" / "franco.db")
        port = free_port()
        env = {**os.environ, "PYTHONPATH": str(sandbox)}
        server = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, franco.paths as p; "
             f"assert str(p.PROJECT_ROOT) == {str(sandbox.resolve())!r}, p.PROJECT_ROOT; "
             f"from franco.dashboard import start_dashboard; start_dashboard(port={port})"],
            cwd=sandbox, env=env, stdout=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/"
        for _ in range(100):
            if server.poll() is not None:
                raise SystemExit("dashboard failed to start (see output above)")
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.HTTPError:
                time.sleep(0.1)

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            ctx = await browser.new_context(
                viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=SCALE)
            await ctx.add_init_script(
                "try { localStorage.setItem('franco-theme', 'light') } catch {}")
            await ctx.add_init_script(CURSOR_JS)
            rec = Recorder(await ctx.new_page())
            await storyboard(rec, url)
            await browser.close()

        write_gif(rec.frames, out)
        secs = sum(d for _, d in rec.frames) / 1000
        print(f"Wrote {out} — {len(rec.frames)} frames, {secs:.1f}s, "
              f"{out.stat().st_size / 1e6:.1f} MB")
    finally:
        if server:
            server.terminate()
            server.wait()
        if keep:
            print(f"Demo sandbox kept at {sandbox}")
        else:
            shutil.rmtree(sandbox, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "dashboard.gif")
    ap.add_argument("--keep", action="store_true",
                    help="keep the demo sandbox (to open its dashboard yourself)")
    args = ap.parse_args()
    asyncio.run(record(args.out, args.keep))


if __name__ == "__main__":
    main()
