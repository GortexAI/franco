"""State for approved-copy outreach: the v12 migration, suppression,
campaign sign-off, and the preview queries."""

import aiosqlite
import pytest

from tests.phase1_helpers import account_row, import_accounts, make_config, make_writer
from tests.test_outbox_native import state  # noqa: F401  (fixture)


async def columns(db_path, table):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_fresh_db_has_v12_schema(state):
    assert {"value", "kind", "reason", "source", "created_at"} <= await columns(
        state.db_path, "suppressions")
    assert {"campaign_key", "owner", "segment", "hypothesis", "qualified_score"} <= \
        await columns(state.db_path, "companies")
    assert {"lost_reason", "recontact_after"} <= await columns(state.db_path, "prospects")
    assert "campaign_key" in await columns(state.db_path, "campaigns")
    assert "seq_day" in await columns(state.db_path, "outbox")


@pytest.mark.asyncio
async def test_suppression_email_and_domain(state):
    assert await state.add_suppression("www.Acme.com", reason="competitor")
    assert not await state.add_suppression("acme.com")          # idempotent
    hit = await state.suppression_for("Jane@ACME.com")
    assert hit["kind"] == "domain" and hit["value"] == "acme.com"
    await state.add_suppression("jane@acme.com", reason="asked")
    assert (await state.suppression_for("jane@acme.com"))["kind"] == "email"
    assert await state.suppression_for("jane@acme.co") is None
    assert await state.suppression_for("") is None
    assert await state.remove_suppression("acme.com")
    assert await state.suppression_for("lee@acme.com") is None
    assert len(await state.list_suppressions()) == 1


@pytest.mark.asyncio
async def test_signoff_round_trip(state):
    assert await state.get_signoff("M1") is None
    await state.set_signoff("M1", "Hemin")
    s = await state.get_signoff("M1")
    assert s["by"] == "Hemin" and s["at"]
    await state.clear_signoff("M1")
    assert await state.get_signoff("M1") is None


@pytest.mark.asyncio
async def test_preview_queries(state):
    config = make_config()
    await import_accounts(state, [account_row(1, owner="A"), account_row(2, owner="B")])
    await make_writer(state, config).run()
    assert await state.staged_accounts_by_owner("M1") == {"A": 1, "B": 1}
    assert await state.staged_accounts_by_owner("F1") == {}

    rows = await state.preview_rows("M1")
    assert len(rows) == 2 and {r["step"] for r in rows} == {1}
    ctx = rows[0]["context"]
    assert ctx["campaign"] == "M1" and ctx["observation"].startswith("your seller")
    assert ctx["source_url"].startswith("https://co") and ctx["checked"]
    assert await state.preview_rows("F1") == []
    assert len(await state.preview_rows()) == 2

    camp = (await state.get_campaigns_by_status("active"))[0]
    assert await state.campaign_key_for(camp.id) == "M1"
    assert await state.campaign_key_for("") == ""
