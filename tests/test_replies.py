"""Reply handling for approved-copy outreach: quoted text never decides,
opt-outs suppress before they acknowledge, machines don't stop a sequence,
"later" pauses, and one reply stops the whole account."""

import pytest

from franco.integrations.mail_provider import InboundMessage
from franco.models.company import Company
from franco.models.prospect import Prospect
from franco.outreach import build_footer
from tests.phase1_helpers import (
    PAST, Brain, RecordingProvider, make_config, make_handler,
)
from tests.test_outbox_native import state  # noqa: F401  (fixture)

CONFIG = make_config(compliance={"send_opt_out_ack": True})
FOOTER = build_footer(CONFIG.compliance, CONFIG.persona)


async def seed(state, email="jane@acme.com", company_id="", status="contacted"):
    pid = await state.add_prospect(Prospect(
        company_id=company_id, first_name="Jane", last_name="Doe", title="VP",
        company="Acme", email=email, email_status="verified", status=status))
    await state.add_outbox_item(prospect_id=pid, to_email=email, subject="follow-up",
                                body="b", send_at=PAST, status="approved",
                                campaign_id="c1", step=2)
    return pid


async def receive(state, body="", subject="Re: Sponsored listings", headers=None,
                  brain=None, config=CONFIG, sender="jane@acme.com"):
    provider = RecordingProvider()
    provider.inbound = [InboundMessage(provider_id="in1", from_email=sender,
                                       subject=subject, body=body, headers=headers or {},
                                       message_id="<r1@x>")]
    handler = make_handler(state, config, brain=brain, provider=provider)
    assert await handler._run_native()
    return handler


async def replies(state):
    return [i for i in await state.get_outbox(limit=100) if i["kind"] == "reply"]


async def sequence(state):
    return [i for i in await state.get_outbox(limit=100) if i["kind"] == "sequence"]


# ── opt-outs ──


@pytest.mark.asyncio
async def test_our_quoted_footer_is_not_an_opt_out(state):
    pid = await seed(state)
    quoted = "\n".join("> " + line for line in ("Hi Jane,", "", *FOOTER.split("\n")))
    body = ("Sounds interesting, tell me more.\n\n"
            "On Mon, Oct 12, 2026 at 9:00 AM Hemin Joshi <sender@example.com> wrote:\n" + quoted)
    await receive(state, body, brain=Brain(intent="interested"))
    assert (await state.get_prospect(pid)).status == "replied"
    assert await state.suppression_for("jane@acme.com") is None


@pytest.mark.asyncio
async def test_unquoted_footer_pasted_back_is_not_an_opt_out(state):
    pid = await seed(state)
    await receive(state, "Tell me more.\n\n" + FOOTER, brain=Brain(intent="question"))
    assert (await state.get_prospect(pid)).status == "replied"
    assert await state.suppression_for("jane@acme.com") is None


@pytest.mark.asyncio
async def test_real_no_thanks_suppresses_then_acknowledges(state):
    pid = await seed(state)
    await receive(state, "No thanks.", brain=Brain(intent="interested"))
    assert (await state.get_prospect(pid)).status == "opted_out"
    assert (await state.suppression_for("jane@acme.com"))["reason"] == "opt_out"
    [ack] = await replies(state)
    assert ack["body"] == "Understood. I have recorded the request to stop."
    assert ack["subject"] == "Re: Sponsored listings"
    assert ack["in_reply_to"] == "<r1@x>"
    assert {i["status"] for i in await sequence(state)} == {"cancelled"}


@pytest.mark.asyncio
async def test_subject_only_no_thanks_is_an_opt_out(state):
    pid = await seed(state)
    await receive(state, body="", subject="no thanks")
    assert (await state.get_prospect(pid)).status == "opted_out"


@pytest.mark.asyncio
async def test_no_ack_unless_configured(state):
    await seed(state)
    await receive(state, "Please unsubscribe me", config=make_config())
    assert await replies(state) == []
    assert await state.suppression_for("jane@acme.com") is not None


# ── machines and timing ──


@pytest.mark.asyncio
async def test_out_of_office_does_not_stop_the_sequence(state):
    pid = await seed(state)
    await receive(state, "I am away until Monday.", subject="Automatic reply: Sponsored",
                  headers={"Auto-Submitted": "auto-replied"})
    assert (await state.get_prospect(pid)).status == "contacted"
    assert {i["status"] for i in await sequence(state)} == {"approved"}
    assert await replies(state) == []


@pytest.mark.asyncio
async def test_later_pauses_and_records_when(state):
    pid = await seed(state)
    await receive(state, "Not now, maybe after Q1.",
                  brain=Brain(intent="later", reply="Understood.", recontact="2027-01-15"))
    p = await state.get_prospect(pid)
    assert p.status == "paused" and p.recontact_after == "2027-01-15"
    assert {i["status"] for i in await sequence(state)} == {"cancelled"}
    assert [r["body"] for r in await replies(state)] == ["Understood."]


# ── not interested ──


@pytest.mark.asyncio
async def test_not_interested_sends_the_approved_closing_text(state):
    text = "That is useful context. A sponsored pilot is probably premature."
    config = make_config(outreach={"reply_templates": {"not_interested": text}})
    pid = await seed(state)
    await receive(state, "We're all set.", brain=Brain(intent="not_interested"), config=config)
    assert (await state.get_prospect(pid)).status == "lost"
    [closing] = await replies(state)
    assert closing["body"] == text + "\n\nHemin"
    assert closing["status"] == "pending_review"


@pytest.mark.asyncio
async def test_not_interested_without_template_sends_nothing(state):
    pid = await seed(state)
    await receive(state, "We're all set.", brain=Brain(intent="not_interested"))
    assert (await state.get_prospect(pid)).status == "lost"
    assert await replies(state) == []


# ── account-level stop ──


@pytest.mark.asyncio
async def test_one_reply_stops_the_whole_account(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.com"))
    await seed(state, "jane@acme.com", company_id=cid)
    other = await seed(state, "lee@acme.com", company_id=cid, status="queued")
    elsewhere = await seed(state, "sam@other.com")
    await receive(state, "Who is this?", brain=Brain(intent="question"))
    by_email = {i["to_email"]: i["status"] for i in await sequence(state)}
    assert by_email == {"jane@acme.com": "cancelled", "lee@acme.com": "cancelled",
                        "sam@other.com": "approved"}
    assert (await state.get_prospect(other)).status == "queued"
    assert (await state.get_prospect(elsewhere)).status == "contacted"
