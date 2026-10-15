"""Import hand-researched accounts: one row per account and buyer.

Each row brings a company, the one person to contact there, the approved
campaign they get, and the fact that justifies contacting them — with the
page it came from and the date it was checked. That fact becomes the
account's TRIGGER_OBSERVATION and fills {{verified_observation}}; a row
without one is rejected, because an account without a sourced reason to
write is never emailed.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from franco.models.company import Company
from franco.models.prospect import Prospect
from franco.outreach import TRIGGER_SIGNAL, clean_observation
from franco.signals import seed_signal_catalog

REQUIRED = (
    "company", "domain", "campaign", "first_name", "last_name", "title",
    "email", "observation", "source_url", "source_date",
)
OPTIONAL = ("owner", "segment", "hypothesis", "score", "email_status", "linkedin_url")
COLUMNS = REQUIRED + OPTIONAL

# Spreadsheet headers people actually write, mapped to ours.
ALIASES = {
    "account": "company", "company_name": "company", "website": "domain",
    "public_url": "domain", "campaign_key": "campaign", "first": "first_name",
    "last": "last_name", "buyer_role": "title", "role": "title",
    "verified_observation": "observation", "public_observation": "observation",
    "observed_trigger": "observation", "trigger": "observation",
    "evidence_url": "source_url", "trigger_source": "source_url",
    "evidence_link": "source_url", "source_date_checked": "source_date",
    "date_checked": "source_date", "checked": "source_date",
    "qualified_score": "score", "score_0_10": "score",
}

EMAIL_STATUSES = {"verified", "risky", "guess", "invalid"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

TEMPLATE = (
    ",".join(COLUMNS) + "\n"
    "Example Market,example-market.com,M1,Dana,Whitfield,CEO,dana@example-market.com,"
    "\"your seller help center describes a paid Featured Listing upgrade\","
    "https://example-market.com/help/featured,2026-10-14,Hemin,Vertical marketplace,"
    "\"Flat featured fee; no relevance or measurement story\",8,,\n"
)


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)      # emails
    rejected: list[tuple[int, str]] = field(default_factory=list)  # (row, why)
    warnings: list[tuple[int, str]] = field(default_factory=list)


def _norm_header(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return ALIASES.get(key, key)


def read_rows(text: str) -> list[dict[str, str]]:
    """CSV text → rows keyed by our column names (aliases resolved)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    rows = []
    for raw in reader:
        row: dict[str, str] = {}
        for k, v in raw.items():
            if k is None:
                continue
            row[_norm_header(k)] = (v or "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


def _clean_domain(value: str) -> str:
    d = re.sub(r"^https?://", "", (value or "").strip().lower())
    return d.split("/", 1)[0].removeprefix("www.")


def _parse_date(value: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return None


def validate_row(row: dict[str, str], campaigns: set[str]) -> tuple[list[str], list[str]]:
    """(problems that reject the row, warnings that don't)."""
    problems, warnings = [], []
    missing = [c for c in REQUIRED if not row.get(c)]
    if missing:
        problems.append("missing " + ", ".join(missing))
    campaign = row.get("campaign", "")
    if campaign and campaign not in campaigns:
        known = ", ".join(sorted(campaigns)) or "none configured"
        problems.append(f"campaign '{campaign}' is not in outreach.campaigns ({known})")
    email = row.get("email", "").lower()
    if email and not EMAIL_RE.match(email):
        problems.append(f"invalid email '{email}'")
    domain = _clean_domain(row.get("domain", ""))
    if email and domain and not (email.endswith("@" + domain) or email.endswith("." + domain)):
        warnings.append(f"email domain differs from company domain {domain}")
    url = row.get("source_url", "")
    if url and not url.startswith(("http://", "https://")):
        problems.append("source_url must be a full http(s) link to the page you checked")
    if row.get("source_date"):
        checked = _parse_date(row["source_date"])
        if checked is None:
            problems.append(f"source_date '{row['source_date']}' is not a date (use YYYY-MM-DD)")
        elif checked > date.today():
            problems.append("source_date is in the future")
    if row.get("observation") and len(clean_observation(row["observation"])) < 12:
        problems.append("observation is too short to be a specific, checkable fact")
    score = row.get("score", "")
    if score:
        try:
            if not 0 <= int(score) <= 10:
                raise ValueError
        except ValueError:
            problems.append(f"score '{score}' must be a whole number 0-10")
    status = row.get("email_status", "").lower()
    if status and status not in EMAIL_STATUSES:
        problems.append(f"email_status must be one of {sorted(EMAIL_STATUSES)}")
    return problems, warnings


async def import_accounts(
    state,
    rows: list[dict[str, str]],
    campaigns: set[str],
    default_owner: str = "",
    verify=None,
    dry_run: bool = False,
) -> ImportReport:
    """Validate every row, then record the valid ones.

    ``verify``: optional ``async (email) -> status | None`` used for rows
    not already marked verified. ``dry_run`` validates without writing.
    """
    report = ImportReport()
    if not dry_run:
        await seed_signal_catalog(state)
    seen_emails: set[str] = set()

    for n, row in enumerate(rows, start=2):  # row 1 is the header
        problems, warnings = validate_row(row, campaigns)
        email = row.get("email", "").lower()
        domain = _clean_domain(row.get("domain", ""))
        owner = row.get("owner") or default_owner

        if not problems:
            if email in seen_emails:
                problems.append("duplicate email in this file")
            blocked = await state.suppression_for(email)
            if blocked:
                problems.append(f"suppressed ({blocked['kind']}: {blocked['reason'] or 'listed'})")
            existing_company = await state.get_company_by_domain(domain)
            if existing_company and existing_company.owner and owner \
                    and existing_company.owner != owner:
                problems.append(f"account already owned by {existing_company.owner}")
            existing = await state.get_prospect_by_email(email)
            if existing and existing.status != "new":
                problems.append(f"already in outreach (status '{existing.status}')")

        for w in warnings:
            report.warnings.append((n, w))
        if problems:
            report.rejected.append((n, "; ".join(problems)))
            continue
        seen_emails.add(email)
        if dry_run:
            report.imported.append(email)
            continue

        score = int(row["score"]) if row.get("score") else None
        company_id = await state.add_company(Company(
            name=row["company"], domain=domain, website=f"https://{domain}",
            industry=row.get("segment", ""), source="import",
            source_url=row["source_url"],
        ))
        await state.update_company_account(
            company_id, campaign_key=row["campaign"], owner=owner,
            segment=row.get("segment", ""), hypothesis=row.get("hypothesis", ""),
            qualified_score=score,
        )

        observation = clean_observation(row["observation"])
        checked = _parse_date(row["source_date"]).isoformat() + " 00:00:00"
        latest = await state.latest_observation(company_id, TRIGGER_SIGNAL)
        if not (latest and latest["value_text"] == observation
                and latest["evidence_url"] == row["source_url"]):
            await state.add_observation(
                TRIGGER_SIGNAL, company_id=company_id, collector="import",
                value_text=observation, evidence_url=row["source_url"],
                observed_at=checked, confidence=1.0,
            )

        status = (row.get("email_status") or "guess").lower()
        if verify is not None and status != "verified":
            status = await verify(email) or status
        prospect_id = await state.add_prospect(Prospect(
            company_id=company_id, first_name=row["first_name"],
            last_name=row["last_name"], title=row["title"], email=email,
            email_status=status, email_verified=(status == "verified"),
            linkedin_url=row.get("linkedin_url", ""), source="import",
            source_url=row["source_url"], company=row["company"],
            industry=row.get("segment", ""),
            score=(score * 10 if score is not None else 0),
        ))
        # A re-import refreshes what research can change about the contact.
        await state.update_prospect_email(prospect_id, email, status)
        report.imported.append(email)
    return report
