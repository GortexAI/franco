"""CSV lists: researched accounts, contact lists and suppressions."""

from datetime import date, timedelta

import aiosqlite
import pytest

from franco.importer import (
    TEMPLATES, import_accounts, import_contacts, import_suppressions, read_rows,
    read_suppressions, run_import, suppressions_csv,
)
from franco.models.prospect import Prospect
from franco.outreach import TRIGGER_SIGNAL
from tests.phase1_helpers import OBSERVATION, account_row
from tests.test_outbox_native import state  # noqa: F401  (fixture)

CAMPAIGNS = {"M1", "F1"}


async def count(state, table):
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            return (await cur.fetchone())[0]


def rejected_reason(report, row=2):
    return dict(report.rejected).get(row, "")


# ── accounts: validation ──


@pytest.mark.asyncio
@pytest.mark.parametrize("change, expect", [
    ({"title": ""}, "missing title"),
    ({"campaign": "Z9"}, "campaign 'Z9' is not"),
    ({"email": "not-an-email"}, "invalid email"),
    ({"source_url": "co1.com/help"}, "source_url must be a full http(s) link"),
    ({"source_date": (date.today() + timedelta(days=3)).isoformat()}, "in the future"),
    ({"source_date": "yesterday"}, "is not a date"),
    ({"observation": "has ads"}, "too short"),
    ({"score": "11"}, "must be a whole number 0-10"),
    ({"score": "x"}, "must be a whole number 0-10"),
    ({"email_status": "great"}, "email_status must be one of"),
])
async def test_account_row_rejections(state, change, expect):
    report = await import_accounts(state, [account_row(1, **change)], CAMPAIGNS)
    assert report.imported == []
    assert expect in rejected_reason(report)


@pytest.mark.asyncio
async def test_account_rejected_when_suppressed(state):
    await state.add_suppression("co1.com", reason="asked")
    report = await import_accounts(state, [account_row(1)], CAMPAIGNS)
    assert "suppressed (domain: asked)" in rejected_reason(report)


@pytest.mark.asyncio
async def test_account_rejected_when_owned_by_someone_else(state):
    await import_accounts(state, [account_row(1, owner="Alex")], CAMPAIGNS)
    report = await import_accounts(state, [account_row(1, owner="Hemin",
                                                       email="other@co1.com")], CAMPAIGNS)
    assert "already owned by Alex" in rejected_reason(report)


@pytest.mark.asyncio
async def test_account_rejected_when_already_in_outreach(state):
    await state.add_prospect(Prospect(first_name="Dana", last_name="W1", email="dana1@co1.com",
                                      status="contacted"))
    report = await import_accounts(state, [account_row(1)], CAMPAIGNS)
    assert "already in outreach (status 'contacted')" in rejected_reason(report)


@pytest.mark.asyncio
async def test_duplicate_email_in_one_file(state):
    report = await import_accounts(state, [account_row(1), account_row(1)], CAMPAIGNS)
    assert report.imported == ["dana1@co1.com"]
    assert rejected_reason(report, 3) == "duplicate email in this file"


@pytest.mark.asyncio
async def test_email_domain_mismatch_is_only_a_warning(state):
    report = await import_accounts(state, [account_row(1, email="dana@gmail.com")], CAMPAIGNS)
    assert report.imported == ["dana@gmail.com"]
    assert "differs from company domain" in report.warnings[0][1]


# ── accounts: what gets written ──


def test_header_aliases():
    text = ("Account,Website,Campaign_Key,First,Last,Role,Email,Trigger,Evidence URL,"
            "Date Checked,Score (0-10)\n"
            "Co,https://www.co.com/,M1,Dana,W,CEO,dana@co.com,obs,https://co.com,2026-10-01,7\n")
    row = read_rows("﻿" + text)[0]
    assert row["company"] == "Co" and row["domain"] == "https://www.co.com/"
    assert row["campaign"] == "M1" and row["title"] == "CEO"
    assert row["observation"] == "obs" and row["source_url"] == "https://co.com"
    assert row["source_date"] == "2026-10-01" and row["score"] == "7"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(state):
    report = await import_accounts(state, [account_row(1)], CAMPAIGNS, dry_run=True)
    assert report.imported == ["dana1@co1.com"]
    assert await count(state, "companies") == 0
    assert await count(state, "prospects") == 0


@pytest.mark.asyncio
async def test_import_writes_account_observation_and_prospect(state):
    checked = (date.today() - timedelta(days=2)).isoformat()
    report = await import_accounts(state, [account_row(
        1, domain="https://www.Co1.com/about", score="8", segment="Marketplace",
        hypothesis="flat featured fee", observation="Your help center sells a Featured upgrade.",
        source_date=checked)], CAMPAIGNS, default_owner="ignored")
    assert report.imported == ["dana1@co1.com"]

    company = await state.get_company_by_domain("co1.com")
    assert company.campaign_key == "M1" and company.owner == "Hemin"
    assert company.segment == "Marketplace" and company.qualified_score == 8
    obs = await state.latest_observation(company.id, TRIGGER_SIGNAL)
    assert obs["value_text"] == "your help center sells a Featured upgrade"
    assert obs["evidence_url"] == "https://co1.com/help"
    assert str(obs["observed_at"]).startswith(checked)
    p = await state.get_prospect_by_email("dana1@co1.com")
    assert p.company_id == company.id and p.email_status == "verified" and p.score == 80


@pytest.mark.asyncio
async def test_reimport_does_not_duplicate_the_observation(state):
    await import_accounts(state, [account_row(1)], CAMPAIGNS)
    await import_accounts(state, [account_row(1)], CAMPAIGNS)
    assert await count(state, "observations") == 1


@pytest.mark.asyncio
async def test_default_owner_and_verify_callback(state):
    async def verify(email):
        return "risky"
    await import_accounts(state, [account_row(1, owner="", email_status="")], CAMPAIGNS,
                          default_owner="Hemin", verify=verify)
    company = await state.get_company_by_domain("co1.com")
    assert company.owner == "Hemin"
    assert (await state.get_prospect_by_email("dana1@co1.com")).email_status == "risky"


# ── contacts ──


@pytest.mark.asyncio
async def test_contacts_company_only_and_person_rows(state):
    text = TEMPLATES["contacts"]
    report = await import_contacts(state, read_rows(text))
    assert report.rejected == []
    assert report.imported == ["dana@example-market.com", "another.example"]
    assert await state.get_company_by_domain("another.example") is not None
    assert await count(state, "prospects") == 1
    p = await state.get_prospect_by_email("dana@example-market.com")
    assert p.status == "new" and p.email_status == "guess"
    company = await state.get_company_by_domain("example-market.com")
    assert company.location == "Austin, TX"


@pytest.mark.asyncio
async def test_contacts_rejections(state):
    await state.add_suppression("blocked.com")
    rows = [
        {"company": "A", "domain": "nodot"},
        {"company": "", "domain": "b.com"},
        {"company": "C", "domain": "c.com", "email": "bad"},
        {"company": "D", "domain": "blocked.com"},
        {"company": "E", "domain": "e.com", "first_name": "Only"},
        {"company": "F", "domain": "f.com", "linkedin_url": "linkedin.com/in/x"},
    ]
    report = await import_contacts(state, rows)
    why = dict(report.rejected)
    assert "invalid domain" in why[2]
    assert "missing company" in why[3]
    assert "invalid email" in why[4]
    assert "suppressed" in why[5]
    assert "first and last name" in why[6]
    assert "https://" in why[7]
    assert report.imported == []
    assert await count(state, "companies") == 0


@pytest.mark.asyncio
async def test_contacts_dry_run_writes_nothing(state):
    report = await import_contacts(state, read_rows(TEMPLATES["contacts"]), dry_run=True)
    assert len(report.imported) == 2
    assert await count(state, "companies") == 0


# ── suppressions ──


@pytest.mark.asyncio
async def test_suppressions_headerless_file(state):
    rows = read_suppressions("a@b.com\nc.org\n")
    report = await import_suppressions(state, rows, default_reason="list")
    assert report.imported == ["a@b.com", "c.org"]
    assert (await state.suppression_for("x@c.org"))["reason"] == "list"


@pytest.mark.asyncio
async def test_suppressions_with_header_reason_and_bad_values(state):
    text = "Email,Reason\nA@B.com,asked to stop\nhttps://www.Shop.com/x,competitor\nnot a value,\n,\n"
    report = await import_suppressions(state, read_suppressions(text))
    assert report.imported == ["a@b.com", "shop.com"]
    why = dict(report.rejected)
    assert "is not an email or a domain" in why[4]
    assert (await state.suppression_for("a@b.com"))["reason"] == "asked to stop"


@pytest.mark.asyncio
async def test_suppressions_already_listed_is_a_warning(state):
    await state.add_suppression("a@b.com")
    report = await import_suppressions(state, [{"value": "a@b.com"}])
    assert report.imported == [] and report.rejected == []
    assert "already suppressed" in report.warnings[0][1]
    dry = await import_suppressions(state, [{"value": "a@b.com"}, {"value": "new.com"}],
                                    dry_run=True)
    assert dry.imported == ["new.com"]
    assert await state.suppression_for("x@new.com") is None


@pytest.mark.asyncio
async def test_suppressions_csv_round_trip_with_commas(state):
    await state.add_suppression("a@b.com", reason="said: stop, please", source="reply")
    text = suppressions_csv(await state.list_suppressions())
    assert '"said: stop, please"' in text
    rows = read_suppressions(text)
    assert rows[0]["value"] == "a@b.com" and rows[0]["reason"] == "said: stop, please"


# ── dispatch ──


@pytest.mark.asyncio
async def test_run_import_dispatch(state):
    acc = await run_import(state, "accounts", TEMPLATES["accounts"], CAMPAIGNS, dry_run=True)
    assert acc.rejected == [] and len(acc.imported) == 1
    sup = await run_import(state, "suppressions", TEMPLATES["suppressions"])
    assert sup.imported == ["someone@example.com", "competitor.example"]
    with pytest.raises(ValueError):
        await run_import(state, "people", "x")


def test_account_template_columns_match_required():
    from franco.importer import COLUMNS
    assert TEMPLATES["accounts"].splitlines()[0].split(",") == list(COLUMNS)
    assert OBSERVATION  # helper sanity
