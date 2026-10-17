"""Delivery reports are only acted on when they match an email Franco sent.

Anyone can send a message that looks like a bounce. Acting on one marks an
address invalid and moves the bounce rate toward the kill switch, so a
spoofed report must change nothing.
"""

from datetime import datetime, timedelta, timezone

import pytest

from franco.integrations.mail_provider import InboundMessage, parse_bounce, referenced_ids
from tests.test_outbox_native import (  # noqa: F401  (state is a fixture)
    FakeProvider, make_handler, seed_prospect, state,
)

GMAIL_FAILURE = """** Address not found **

Your message wasn't delivered to jane@acme.com because the address couldn't be
found, or is unable to receive mail.
"""
RFC3464_FAILURE = """Reporting-MTA: dns; mx.acme.com
Final-Recipient: rfc822; jane@acme.com
Action: failed
Status: 5.1.1
"""
RFC3464_DELAY = """Final-Recipient: rfc822; jane@acme.com
Action: delayed
Status: 4.2.2
"""


def _now(days_ago=0):
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)).isoformat()


async def _sent(state, pid, to="jane@acme.com", message_id="<m1@x>", days_ago=0, campaign="c1"):
    oid = await state.add_outbox_item(prospect_id=pid, to_email=to, subject="s", body="b",
                                      send_at=_now(), status="approved", campaign_id=campaign, step=1)
    await state.update_outbox_item(oid, status="sent", message_id=message_id, sent_at=_now(days_ago))
    return oid


def _report(body, subject="Delivery Status Notification (Failure)", in_reply_to="", references=""):
    return InboundMessage(provider_id=f"b-{abs(hash(body + in_reply_to + references))}",
                          from_email="mailer-daemon@googlemail.com", subject=subject, body=body,
                          in_reply_to=in_reply_to, is_bounce=True,
                          headers={"references": references} if references else {})


async def _run(state, *messages):
    provider = FakeProvider()
    provider.inbound = list(messages)
    await make_handler(state, provider)._run_native()


# ── parsing ──


def test_parse_reads_dsn_fields_and_gmail_text():
    assert parse_bounce(_report(RFC3464_FAILURE)) .recipients == {"jane@acme.com"}
    assert parse_bounce(_report(RFC3464_FAILURE)).permanent
    assert parse_bounce(_report(GMAIL_FAILURE)).recipients == {"jane@acme.com"}
    assert parse_bounce(_report(GMAIL_FAILURE)).permanent
    delay = parse_bounce(_report(RFC3464_DELAY, subject="Delivery Status Notification (Delay)"))
    assert not delay.permanent
    assert not parse_bounce(_report("Delivery incomplete. We'll keep trying to deliver it.")).permanent


def test_referenced_ids_include_references_header():
    msg = _report("x", in_reply_to="<a@x>", references="<a@x> <b@x>")
    assert referenced_ids(msg) == ["<a@x>", "<b@x>"]


# ── acting on reports ──


@pytest.mark.asyncio
async def test_real_bounce_by_message_id_marks_invalid(state):
    pid = await seed_prospect(state, status="contacted")
    oid = await _sent(state, pid)
    await _run(state, _report(GMAIL_FAILURE, in_reply_to="<m1@x>"))
    assert (await state.get_prospect(pid)).email_status == "invalid"
    assert (await state.get_outbox_item(oid))["error"] == "bounced"
    assert await state.get_setting("bounce_count") == "1"


@pytest.mark.asyncio
async def test_real_bounce_found_through_references_or_recent_send(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid, message_id="<m9@x>")
    await _run(state, _report(RFC3464_FAILURE, references="<other@x> <m9@x>"))
    assert (await state.get_prospect(pid)).email_status == "invalid"

    pid2 = await seed_prospect(state, email="bob@beta.com", status="contacted")
    await _sent(state, pid2, to="bob@beta.com", message_id="<n1@x>", campaign="c2")
    await _run(state, _report("Your message wasn't delivered to bob@beta.com."))
    assert (await state.get_prospect(pid2)).email_status == "invalid"


@pytest.mark.asyncio
async def test_spoofed_bounce_for_nothing_we_sent_changes_nothing(state):
    pid = await seed_prospect(state, status="contacted")
    for i in range(12):
        await _sent(state, pid, message_id=f"<m{i}@x>", campaign=f"c{i}")
    spoofs = [_report(f"Your message wasn't delivered to stranger{i}@nowhere.example",
                      in_reply_to=f"<forged{i}@x>") for i in range(5)]
    await _run(state, *spoofs)
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert await state.get_setting("bounce_count", "0") == "0"
    assert await state.get_setting("sending_paused") == ""


@pytest.mark.asyncio
async def test_bounce_naming_a_different_recipient_is_ignored(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid)
    await _run(state, _report("Your message wasn't delivered to someone@else.example",
                              in_reply_to="<m1@x>"))
    assert (await state.get_prospect(pid)).email_status == "verified"


@pytest.mark.asyncio
async def test_old_sends_are_not_matched_by_address_alone(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid, days_ago=30)
    await _run(state, _report(GMAIL_FAILURE))
    assert (await state.get_prospect(pid)).email_status == "verified"


@pytest.mark.asyncio
async def test_temporary_delay_is_not_a_bounce(state):
    pid = await seed_prospect(state, status="contacted")
    oid = await _sent(state, pid)
    await _run(state, _report(RFC3464_DELAY, subject="Delivery Status Notification (Delay)",
                              in_reply_to="<m1@x>"))
    assert (await state.get_prospect(pid)).email_status == "verified"
    assert (await state.get_outbox_item(oid))["error"] in ("", None)


@pytest.mark.asyncio
async def test_the_same_bounce_counts_once(state):
    pid = await seed_prospect(state, status="contacted")
    await _sent(state, pid)
    first = _report(GMAIL_FAILURE, in_reply_to="<m1@x>")
    second = _report(GMAIL_FAILURE + "\n(resent)", in_reply_to="<m1@x>")
    await _run(state, first, second)
    assert await state.get_setting("bounce_count") == "1"
