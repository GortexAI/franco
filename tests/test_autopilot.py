"""Autopilot: a runtime toggle over channels.email.require_approval.

Autopilot decides only whether a human reviews a draft. Everything else
that guards a send — the gate, suppression, the kill switch — still runs.
"""

import pytest

from franco.integrations.mail_provider import InboundMessage
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, _now_iso, make_handler, make_sender, seed_campaign,
    seed_prospect, state,
)


# ── precedence ──


@pytest.mark.asyncio
async def test_unset_follows_config(state):
    assert await state.get_autopilot() == ""
    assert await state.approval_required(True) is True
    assert await state.approval_required(False) is False


@pytest.mark.asyncio
async def test_override_beats_config(state):
    await state.set_autopilot("on")
    assert await state.approval_required(True) is False
    await state.set_autopilot("off")
    assert await state.approval_required(False) is True
    await state.set_autopilot("")
    assert await state.approval_required(True) is True


@pytest.mark.asyncio
async def test_rejects_unknown_value(state):
    with pytest.raises(ValueError):
        await state.set_autopilot("maybe")


# ── staging ──


@pytest.mark.asyncio
async def test_autopilot_on_stages_approved(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await state.set_autopilot("on")
    sender = make_sender(state, FakeProvider(), require_approval=True)
    await sender._run_native()
    assert await state.get_outbox(status="pending_review") == []
    # Step 1 is due now and goes out in the same pass; step 2 waits, approved.
    assert len(await state.get_outbox(status="sent")) == 1
    assert len(await state.get_outbox(status="approved")) == 1


@pytest.mark.asyncio
async def test_autopilot_off_overrides_config_autopilot(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await state.set_autopilot("off")
    sender = make_sender(state, FakeProvider(), require_approval=False)
    await sender._run_native()
    assert await state.get_outbox(status="approved") == []
    assert len(await state.get_outbox(status="pending_review")) == 2


@pytest.mark.asyncio
async def test_autopilot_still_respects_kill_switch(state):
    pid = await seed_prospect(state)
    await seed_campaign(state, [pid])
    await state.set_autopilot("on")
    await state.set_setting("sending_paused", "test")
    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=True)
    await sender._run_native()
    assert provider.sent == []
    assert await state.get_outbox(status="sent") == []


@pytest.mark.asyncio
async def test_autopilot_on_approves_replies(state):
    await seed_prospect(state, status="contacted")
    provider = FakeProvider()
    provider.inbound = [InboundMessage(
        provider_id="in1", from_email="jane@acme.com",
        subject="Re: hi", body="How does pricing work?",
    )]
    await state.set_autopilot("on")
    handler = make_handler(state, provider, intent="question")
    await handler._run_native()
    replies = [i for i in await state.get_outbox(status="approved") if i["kind"] == "reply"]
    assert replies
    assert await state.get_outbox(status="pending_review") == []


# ── dashboard ──


def test_dashboard_toggle_approves_pending(monkeypatch):
    import asyncio
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    import franco.dashboard as dash
    from franco.state import StateManager

    monkeypatch.setattr(dash, "_config_requires_approval", lambda: True)
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "franco.db"
        monkeypatch.setattr(dash, "DB_PATH", db)

        async def seed():
            sm = StateManager(str(db))
            await sm.init_db()
            pid = await seed_prospect(sm)
            await sm.add_outbox_item(
                prospect_id=pid, to_email="jane@acme.com", subject="hi",
                body="b", send_at=_now_iso(), status="pending_review",
                campaign_id="c1", step=1,
            )
        asyncio.run(seed())

        with TestClient(dash.app, base_url="http://localhost") as c:
            assert c.get("/api/outbox").json()["autopilot"]["on"] is False
            r = c.post("/api/autopilot/on").json()
            assert r["success"] and r["approved"] == 1
            data = c.get("/api/outbox").json()
            assert data["autopilot"] == {"on": True, "override": "on", "config_on": False}
            assert data["pending"] == []

            c.post("/api/autopilot/off")
            assert c.get("/api/outbox").json()["autopilot"]["on"] is False
            c.post("/api/autopilot/default")
            assert c.get("/api/outbox").json()["autopilot"]["override"] == ""
            assert c.post("/api/autopilot/bogus").status_code == 400


# ── the dashboard only takes orders from its own page ──


def test_dashboard_refuses_cross_site_and_rebound_requests(monkeypatch):
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    import franco.dashboard as dash

    monkeypatch.setattr(dash, "_config_requires_approval", lambda: True)
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(dash, "DB_PATH", Path(tmp) / "franco.db")
        with TestClient(dash.app, base_url="http://localhost:5555") as c:
            # A page on another site POSTing to localhost (a form, or fetch).
            r = c.post("/api/autopilot/on", headers={"Origin": "https://evil.example"})
            assert r.status_code == 403
            r = c.post("/api/autopilot/on", headers={"Sec-Fetch-Site": "cross-site"})
            assert r.status_code == 403
            # Same port is part of the origin.
            r = c.post("/api/autopilot/on", headers={"Origin": "http://localhost:9999"})
            assert r.status_code == 403
            assert c.get("/api/outbox").json()["autopilot"]["on"] is False

            # DNS rebinding: a foreign hostname resolving to 127.0.0.1.
            r = c.get("/api/outbox", headers={"Host": "evil.example:5555"})
            assert r.status_code == 403

            # The dashboard's own page, and non-browser clients, still work.
            r = c.post("/api/autopilot/off", headers={"Origin": "http://localhost:5555"})
            assert r.status_code == 200
            assert c.post("/api/autopilot/default").status_code == 200

        with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
            r = c.post("/api/autopilot/off", headers={"Origin": "http://127.0.0.1:5555"})
            assert r.status_code == 200

        monkeypatch.setenv("FRANCO_DASHBOARD_HOSTS", "franco.lan")
        with TestClient(dash.app, base_url="http://franco.lan:5555") as c:
            assert c.get("/api/outbox").status_code == 200
