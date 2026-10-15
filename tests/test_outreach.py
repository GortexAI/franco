"""Pure approved-copy helpers (franco/outreach.py) and the gate's
compliance checks. No I/O, so every rule here is cheap to pin down."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from franco.gate import pre_send_check
from franco.models.prospect import Prospect
from franco.outreach import (
    PREVIEW_CSV_COLUMNS, add_business_days, build_footer, clean_observation,
    compliance_problem, first_send_day, local_date, preview_record, render,
    send_at_utc, strip_quoted, unsubscribe_headers, with_footer,
)
from tests.phase1_helpers import ADDRESS, TWO_LINE_FOOTER

PERSONA = SimpleNamespace(name="Hemin Joshi", company="Gortex", email="sender@example.com")


def compliance(**kw):
    base = dict(require_footer=True, postal_address=ADDRESS, unsubscribe_url="",
                footer="{sender_name} | {company} | {postal_address}. Business outreach "
                       "about {company}. Reply \"no thanks\" to stop.")
    base.update(kw)
    return SimpleNamespace(**base)


# ── render ──


def test_render_fills_known_fields():
    assert render("Hi {{first_name}} at {{ company }}", {"first_name": "Dana", "company": "Acme"}) \
        == "Hi Dana at Acme"


def test_render_leaves_unknown_and_empty_fields_for_the_gate():
    out = render("Hi {{first_name}}, {{unknown}} {{ title }}", {"first_name": "Dana", "title": "  "})
    assert out == "Hi Dana, {{unknown}} {{ title }}"


# ── clean_observation ──


def test_clean_observation_reads_after_i_noticed():
    assert clean_observation("Your seller help center describes a paid upgrade.") \
        == "your seller help center describes a paid upgrade"
    assert clean_observation("  The   site  lists  boosts!  ") == "the site lists boosts"


def test_clean_observation_keeps_names_and_acronyms():
    assert clean_observation("Etsy runs Offsite Ads.") == "Etsy runs Offsite Ads"
    assert clean_observation("NASA posts jobs") == "NASA posts jobs"
    assert clean_observation("") == ""


# ── footer ──


def test_build_footer_one_line_default():
    assert build_footer(compliance(), PERSONA) == (
        f"Hemin Joshi | Gortex | {ADDRESS}. Business outreach about Gortex. "
        "Reply \"no thanks\" to stop.")


def test_build_footer_two_line_gortex_format():
    assert build_footer(compliance(footer=TWO_LINE_FOOTER), PERSONA) == (
        f"Hemin Joshi | Gortex | {ADDRESS}\n"
        "Business outreach about Gortex. Reply \"no thanks\" to stop.")


def test_build_footer_empty_when_not_required():
    assert build_footer(compliance(require_footer=False), PERSONA) == ""


def test_with_footer_appends_once():
    footer = build_footer(compliance(footer=TWO_LINE_FOOTER), PERSONA)
    body = with_footer("Hi Dana,\n\nHemin\n\n", footer)
    assert body == "Hi Dana,\n\nHemin\n\n" + footer
    assert with_footer(body, footer) == body
    assert with_footer("Body  ", "") == "Body"


# ── unsubscribe headers ──


def test_unsubscribe_mailto_always():
    assert unsubscribe_headers(compliance(), PERSONA) == {
        "List-Unsubscribe": "<mailto:sender@example.com?subject=unsubscribe>"}


def test_unsubscribe_one_click_only_for_https():
    h = unsubscribe_headers(compliance(unsubscribe_url="https://gortex.ai/u"), PERSONA)
    assert h["List-Unsubscribe"] == "<https://gortex.ai/u>, <mailto:sender@example.com?subject=unsubscribe>"
    assert h["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    plain = unsubscribe_headers(compliance(unsubscribe_url="http://gortex.ai/u"), PERSONA)
    assert "List-Unsubscribe-Post" not in plain
    assert "http://" not in plain["List-Unsubscribe"]


def test_unsubscribe_none_without_footer_requirement():
    assert unsubscribe_headers(compliance(require_footer=False), PERSONA) == {}
    assert unsubscribe_headers(None, PERSONA) == {}


# ── business days ──


def test_add_business_days_skips_weekends():
    fri = date(2026, 10, 16)
    assert add_business_days(fri, 1) == date(2026, 10, 19)   # Monday
    assert add_business_days(fri, 5) == date(2026, 10, 23)
    assert add_business_days(date(2026, 10, 14), 4) == date(2026, 10, 20)  # Wed + 4


def test_add_business_days_zero_rolls_weekend_forward():
    assert add_business_days(date(2026, 10, 17), 0) == date(2026, 10, 19)  # Sat → Mon
    assert add_business_days(date(2026, 10, 15), 0) == date(2026, 10, 15)


def test_first_send_day():
    mon = datetime(2026, 10, 19, 8, 0)
    assert first_send_day(mon, 9) == date(2026, 10, 19)
    assert first_send_day(mon.replace(hour=9), 9) == date(2026, 10, 20)
    assert first_send_day(datetime(2026, 10, 16, 10, 0), 9) == date(2026, 10, 19)  # Fri late


def test_first_send_day_on_weekend_is_monday():
    assert first_send_day(datetime(2026, 10, 17, 6, 0), 9) == date(2026, 10, 19)   # Saturday
    assert first_send_day(datetime(2026, 10, 18, 20, 0), 9) == date(2026, 10, 19)  # Sunday


def test_send_at_utc_follows_daylight_saving():
    assert send_at_utc(date(2026, 7, 1), 9, "America/Los_Angeles") == "2026-07-01T16:00:00"
    assert send_at_utc(date(2026, 12, 1), 9, "America/Los_Angeles") == "2026-12-01T17:00:00"


def test_local_date_crosses_midnight():
    assert local_date(datetime(2026, 10, 16, 3, 0), "America/Los_Angeles") == date(2026, 10, 15)


# ── strip_quoted ──

FOOTER = build_footer(compliance(footer=TWO_LINE_FOOTER), PERSONA)


def test_strip_gmail_attribution():
    text = ("Sounds good, tell me more.\n\nOn Mon, Oct 12, 2026 at 9:00 AM Hemin Joshi "
            "<sender@example.com> wrote:\n> Hi Dana\n> " + FOOTER.replace("\n", "\n> "))
    assert strip_quoted(text, FOOTER) == "Sounds good, tell me more."


def test_strip_gmail_wrapped_attribution():
    text = ("Interested.\r\n\r\nOn Mon, Oct 12, 2026 at 9:00 AM Hemin Joshi <\r\n"
            "sender@example.com> wrote:\r\nHi Dana, quoted without arrows. Reply \"no thanks\" to stop.")
    assert strip_quoted(text) == "Interested."


def test_strip_outlook_original_message():
    text = "Please call me.\n\n-----Original Message-----\nFrom: Hemin\nno thanks"
    assert strip_quoted(text) == "Please call me."


def test_strip_arrow_lines_and_own_footer():
    text = "Yes please.\n> quoted line\n> another\n\n" + FOOTER
    assert strip_quoted(text, FOOTER) == "Yes please."


def test_strip_keeps_a_real_no_thanks():
    assert strip_quoted("No thanks.\n\nOn Tue, Oct 13 Hemin wrote:\n> hi", FOOTER) == "No thanks."


# ── compliance_problem ──


def test_compliance_problem():
    assert compliance_problem(None) == ""
    assert compliance_problem(compliance(require_footer=False, postal_address="")) == ""
    assert compliance_problem(compliance()) == ""
    assert "postal_address" in compliance_problem(compliance(postal_address="  "))


# ── preview_record ──


def test_preview_record_has_every_column():
    rec = preview_record({
        "campaign_key": "M1", "to_email": "dana@co.com", "subject": "S", "body": "B",
        "status": "pending_review",
        "context": {"owner": "Hemin", "company": "Co", "domain": "co.com", "name": "Dana W",
                    "title": "CEO", "observation": "obs", "source_url": "https://co.com/x",
                    "checked": "2026-10-14"},
    })
    assert set(rec) == set(PREVIEW_CSV_COLUMNS)
    assert rec["buyer"] == "Dana W" and rec["campaign"] == "M1" and rec["checked"] == "2026-10-14"
    assert preview_record({})["company"] == ""


# ── gate ──

PROSPECT = Prospect(first_name="Dana", last_name="W", title="CEO",
                    email="dana@co.com", email_status="verified")


def test_gate_blocks_claims_case_insensitively_in_replies_too():
    for kind in ("sequence", "reply"):
        r = pre_send_check("dana@co.com", "Re: hi", "We run a Real-Time Auction today.",
                           PROSPECT, kind=kind, blocked_phrases=("real-time auction",))
        assert not r.ok and any("blocked claims" in x for x in r.reasons)
    subj = pre_send_check("dana@co.com", "REAL-TIME AUCTION", "fine body",
                          PROSPECT, kind="reply", blocked_phrases=["real-time auction"])
    assert not subj.ok


def test_gate_footer_required_for_cold_mail_only():
    cold = pre_send_check("dana@co.com", "hi", "Body without footer.", PROSPECT,
                          required_footer=FOOTER)
    assert any("compliance footer" in x for x in cold.reasons)
    ok = pre_send_check("dana@co.com", "hi", with_footer("Body.", FOOTER), PROSPECT,
                        required_footer=FOOTER)
    assert ok.ok, ok.reasons
    reply = pre_send_check("dana@co.com", "Re: hi", "Thanks!", PROSPECT, kind="reply",
                           required_footer=FOOTER)
    assert reply.ok, reply.reasons
