"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
from datetime import datetime, timezone

import pytz

from franco.brain import Brain
from franco.config import FrancoConfig
from franco.integrations.mail_provider import NATIVE_PROVIDERS
from franco.models.campaign import Campaign, EmailStep
from franco.outreach import (
    MERGE_FIELD_RE, TRIGGER_SIGNAL, add_business_days, build_footer,
    clean_observation, compliance_problem, first_send_day, render,
    send_at_utc, with_footer,
)
from franco.state import StateManager

logger = logging.getLogger("franco.writer")

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: FrancoConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        outreach = getattr(self.config, "outreach", None)
        if outreach is not None and outreach.mode == "templates":
            await self._run_templates()
            return

        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        deliverable = {"verified", "risky"}
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in deliverable
        ]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Batch prospects into campaign groups (by industry/title for relevance)
        batches = self._group_prospects(prospects_with_email)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            sequence = await self._write_sequence(prospects)
            if not sequence:
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.update_prospect_status(p.id, "queued")

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    # ── Approved copy (outreach.mode: templates) ──

    async def _run_templates(self):
        """Stage approved sequences, word for word, for imported accounts.

        No model is involved. Only {{merge fields}} vary, and
        {{verified_observation}} comes from a sourced, recent observation
        recorded for the account — no observation, no email. One contact
        per account is in a sequence at a time.
        """
        outreach = self.config.outreach
        compliance = self.config.compliance
        problem = compliance_problem(compliance)
        if problem:
            logger.error(f"Writer: not staging approved copy: {problem}.")
            return
        footer = build_footer(compliance, self.config.persona)

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        deliverable = {"verified", "risky"} if allow_risky else {"verified"}
        prospects = sorted(await self.state.get_prospects_by_status("new"),
                           key=lambda p: p.score, reverse=True)

        skipped: dict[str, int] = {}

        def skip(reason: str):
            skipped[reason] = skipped.get(reason, 0) + 1

        claimed: set[str] = set()
        by_campaign: dict[str, list[tuple]] = {}
        for p in prospects:
            if not p.email or (p.email_status or "guess") not in deliverable:
                skip("email not verified")
                continue
            company = await self.state.get_company(p.company_id) if p.company_id else None
            if company is None or not company.campaign_key:
                skip("not an imported account")
                continue
            if company.campaign_key not in outreach.campaigns:
                skip(f"unknown campaign '{company.campaign_key}'")
                continue
            if company.id in claimed:
                skip("another contact at the account is next")
                continue
            contacts = await self.state.get_contacts_for_company(company.id)
            if any(c.id != p.id and c.status != "new" for c in contacts):
                skip("account already in a sequence")
                continue
            blocked = await self.state.suppression_for(p.email)
            if blocked:
                await self.state.update_prospect_status(p.id, "suppressed")
                skip("suppressed")
                continue
            obs = await self.state.latest_observation(
                company.id, TRIGGER_SIGNAL,
                max_age_days=outreach.observation_max_age_days,
            )
            if not obs:
                skip(f"no sourced observation from the last "
                     f"{outreach.observation_max_age_days} days")
                continue
            claimed.add(company.id)
            by_campaign.setdefault(company.campaign_key, []).append((p, company, obs))

        if skipped:
            logger.info("Writer: not staged — " + "; ".join(
                f"{n} × {reason}" for reason, n in skipped.items()))
        if not by_campaign:
            return

        require_approval = await self.state.approval_required(
            getattr(self.config.channels.email, "require_approval", True))
        status = "pending_review" if require_approval else "approved"
        provider = self.config.channels.email.provider
        tz = pytz.timezone(outreach.timezone)
        day1 = first_send_day(datetime.now(tz), outreach.send_hour)

        for key, rows in by_campaign.items():
            spec = outreach.campaigns[key]
            steps = [EmailStep(step=i + 1, subject=s.subject, body=s.body, day=s.day)
                     for i, s in enumerate(spec.steps)]
            campaign_id = await self.state.add_campaign(Campaign(
                id="", name=spec.name or key, channel="email", sequence=steps,
                prospect_ids=[p.id for p, _, _ in rows], status="active",
                campaign_key=key,
            ))
            staged = 0
            for p, company, obs in rows:
                values = {
                    "first_name": p.first_name, "last_name": p.last_name,
                    "company": company.name or p.company, "title": p.title,
                    "verified_observation": clean_observation(obs["value_text"]),
                    "sender_name": self.config.persona.name,
                }
                drafts = [(s, render(s.subject, values), render(s.body, values))
                          for s in steps]
                unfilled = {m for _, subj, body in drafts
                            for m in MERGE_FIELD_RE.findall(subj + body)}
                if unfilled:
                    logger.warning(
                        f"Writer: {p.email} skipped — no value for "
                        f"{sorted(unfilled)}. Fix the import and it will be retried.")
                    continue
                for s, subject, body in drafts:
                    await self.state.add_outbox_item(
                        prospect_id=p.id, campaign_id=campaign_id, step=s.step,
                        to_email=p.email, subject=subject,
                        body=with_footer(body, footer),
                        send_at=send_at_utc(add_business_days(day1, s.day - 1),
                                            outreach.send_hour, outreach.timezone),
                        status=status, provider=provider, seq_day=s.day,
                    )
                await self.state.update_prospect_status(p.id, "queued")
                staged += 1

            await self.state.log_action(
                action_type="write_campaign", agent="writer",
                details={"campaign_id": campaign_id, "campaign_name": spec.name or key,
                         "prospect_count": staged, "steps": len(steps),
                         "mode": "templates"},
            )
            logger.info(
                f"Writer: staged approved copy '{spec.name or key}' for {staged} "
                f"account(s), {len(steps)} emails each "
                f"({'awaiting your approval' if require_approval else 'approved'}).")

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        require_approval = await self.state.approval_required(
            getattr(self.config.channels.email, "require_approval", True))
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        drafted = 0
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if not draft:
                continue
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        facts = [
            f"- Name: {prospect.full_name()}",
            f"- Title: {prospect.title}",
            f"- Company: {prospect.company}",
        ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them. Every claim about the prospect in
your email must come from these facts. If a fact isn't listed here, you
don't know it — do not invent funding rounds, mutual contacts, metrics,
or anything else:
{chr(10).join(facts)}

Requirements:
- Under 75 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words.
- Follow every STRICT EMAIL RULE above.

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="franco-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self.config.persona.name,
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            prompt = f"""You are {self.config.persona.name}, {self.config.persona.role} at {self.config.persona.company}.
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{prospect_summary}

Requirements:
- Email 1: Personalized cold observation + one question. Under 75 words. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping.
- Use {{{{first_name}}}}, {{{{company}}}}, {{{{title}}}} as merge variables — every email
  must use at least one, and email 1 must reference something specific to
  these prospects' industry or role (use the notes above).
- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words.
- Follow every rule in the STRICT EMAIL RULES above. No exceptions.

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="franco-writer",
            agent="writer", task="write_sequence",
        )
        return self._parse_sequence(result)

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list) -> dict[str, list]:
        """Group prospects into campaign batches by industry/title combo."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            # Group by industry + rough title category
            industry = prospect.industry or "general"
            key = f"{industry}-outreach"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
