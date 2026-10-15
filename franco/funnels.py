"""Raad mode: narrow funnels, each with its own story.

Named after Raad Mobrem, CEO of Intro, whose approach to outbound this
follows:

- A business lives by (at most) three customer benefits. Every message
  connects to one of them and never works against another.
- Don't send one broad message. Split the market into narrow funnels you
  can actually observe (a marketplace that already runs AdSense is a
  different conversation from one with no ads at all), the way Zapier
  writes to "Shopify + QuickBooks" shops about exactly that connection.
- Each funnel tells a tiny story every company in it recognises ("every
  marketplace reaches a size where sellers will pay for better placement")
  and then how you handle it, bounded to what is true today.
- The pitch shifts with the buyer: a CEO and a head of product care about
  different consequences of the same thing.
- The email lands on a page that restates the same narrow case.

This module only decides which funnel an account belongs to and collects
the facts behind that decision. The writer turns that into email 1; the
pre-send gate, preview sign-off and compliance rules apply as everywhere.
"""

import re
from dataclasses import dataclass, field

from franco.outreach import TRIGGER_SIGNAL

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Account fields a funnel may match on (lowercase in config).
ACCOUNT_FIELDS = ("segment", "industry", "location", "company_size", "description",
                  "hypothesis", "owner")


@dataclass
class Condition:
    raw: str
    key: str
    value: str = ""
    is_signal: bool = False


@dataclass
class Placement:
    """Which funnel an account is in, and why."""
    funnel: str = ""
    reasons: list[str] = field(default_factory=list)   # the conditions that held
    facts: list[dict] = field(default_factory=list)    # observations behind them


def parse_condition(raw: str) -> Condition:
    key, _, value = (raw or "").partition("=")
    key, value = key.strip(), value.strip()
    if _CODE_RE.match(key):
        return Condition(raw=raw, key=key, value=value, is_signal=True)
    key = key.lower()
    if key not in ACCOUNT_FIELDS:
        raise ValueError(
            f"funnel condition '{raw}': '{key}' is neither a SIGNAL_CODE nor one of "
            f"{', '.join(ACCOUNT_FIELDS)}")
    return Condition(raw=raw, key=key, value=value)


def _observed(obs: dict | None, value: str) -> bool:
    if not obs:
        return False
    text = (obs.get("value_text") or "").lower()
    num = obs.get("value_num")
    if value:
        # "AdSense|Ad Manager|Prebid": any of them
        return any(v.strip().lower() in text or (num is not None and v.strip() == str(num))
                   for v in value.split("|") if v.strip())
    if text:
        return text not in ("0", "false", "no", "none")
    return bool(num)


def holds(cond: Condition, company, facts: dict[str, dict]) -> bool:
    if cond.is_signal:
        return _observed(facts.get(cond.key), cond.value)
    have = str(getattr(company, cond.key, "") or "").lower()
    if not cond.value:
        return bool(have)
    return any(v.strip().lower() in have for v in cond.value.split("|") if v.strip())


async def company_facts(state, company_id: str) -> dict[str, dict]:
    """The newest observation of every signal for one company."""
    rows = await state.get_observations(company_id=company_id, latest_only=True)
    return {r["signal_code"]: r for r in rows}


def place(outreach, company, facts: dict[str, dict]) -> Placement:
    """The first funnel (in config order) whose conditions all hold.

    An account imported with a funnel key keeps it: the researcher's call
    beats inference.
    """
    if company is not None and company.campaign_key in outreach.funnels:
        return Placement(funnel=company.campaign_key, reasons=["assigned at import"],
                         facts=_evidence([], facts))
    for key, funnel in outreach.funnels.items():
        require = [parse_condition(c) for c in funnel.match.get("require", [])]
        exclude = [parse_condition(c) for c in funnel.match.get("exclude", [])]
        if all(holds(c, company, facts) for c in require) and \
                not any(holds(c, company, facts) for c in exclude):
            reasons = [c.raw for c in require] + [f"not {c.raw}" for c in exclude]
            return Placement(funnel=key, reasons=reasons or ["catch-all"],
                             facts=_evidence(require, facts))
    return Placement()


def _evidence(require: list[Condition], facts: dict[str, dict]) -> list[dict]:
    """What we can cite: the signals the funnel matched on, plus any
    researched trigger observation (always first)."""
    out = []
    trigger = facts.get(TRIGGER_SIGNAL)
    if trigger and trigger.get("value_text"):
        out.append(trigger)
    for c in require:
        obs = facts.get(c.key) if c.is_signal else None
        if obs and obs is not trigger:
            out.append(obs)
    return out


def persona_angle(funnel, title: str) -> str:
    for p in funnel.personas:
        if re.search(p.match, title or "", re.IGNORECASE):
            return p.angle
    return ""


def validate_funnels(outreach) -> list[str]:
    """Problems with the funnel conditions (bad codes/fields), for the CLI."""
    problems = []
    for key, funnel in outreach.funnels.items():
        for side in ("require", "exclude"):
            for raw in funnel.match.get(side, []):
                try:
                    parse_condition(raw)
                except ValueError as e:
                    problems.append(f"{key}: {e}")
    return problems


async def funnel_report(state, outreach, limit: int = 5000) -> dict:
    """How the companies Franco knows split across funnels.

    ``funnels[key]``: companies placed there, how many have a contact Franco
    may email (new, verified), and a few example names. ``unplaced``: the
    companies no funnel matches, which is where a new funnel may be hiding.
    """
    out = {key: {"name": f.name or key, "benefit": f.benefit, "companies": 0,
                 "reachable": 0, "examples": [], "landing_url": f.landing_url,
                 "require": f.match.get("require", []), "exclude": f.match.get("exclude", [])}
           for key, f in outreach.funnels.items()}
    unplaced, unplaced_examples = 0, []
    for company_id in await state.list_company_ids(limit):
        company = await state.get_company(company_id)
        if company is None:
            continue
        placement = place(outreach, company, await company_facts(state, company_id))
        if not placement.funnel:
            unplaced += 1
            if len(unplaced_examples) < 5:
                unplaced_examples.append(company.name or company.domain)
            continue
        row = out[placement.funnel]
        row["companies"] += 1
        if len(row["examples"]) < 5:
            row["examples"].append(company.name or company.domain)
        contacts = await state.get_contacts_for_company(company_id)
        if any(c.status == "new" and c.email and c.email_status == "verified" for c in contacts):
            row["reachable"] += 1
    return {"funnels": out, "unplaced": unplaced, "unplaced_examples": unplaced_examples}
