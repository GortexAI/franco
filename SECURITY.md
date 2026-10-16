# Security policy

Franco sends email on your behalf, holds personal data about the people you
contact, and keeps credentials for your mailbox and APIs. We take reports
about it seriously.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Report it privately instead:

1. **Preferred:** [open a private security advisory](https://github.com/GortexAI/franco/security/advisories/new)
   on this repository. Only the maintainers can see it.
2. Or email **hello@gortex.ai** with "Franco security" in the subject.

Please include:
- what an attacker could do, and what they need first;
- steps or a proof of concept that reproduce it;
- the Franco version or commit, and your OS and Python version.

We'll acknowledge your report within 3 business days and keep you updated
until it's resolved. Once a fix ships, we'll credit you in the advisory
unless you'd rather we didn't.

## Supported versions

Fixes go into `main` and the next release. Please make sure the problem
still exists on the latest `main` before you report it.

## What's in scope

Some examples of what we want to hear about:
- the local dashboard taking actions or leaking data to another website or
  host (it should only answer its own page on localhost);
- a way to make Franco send email that skips the pre-send gate, the
  suppression list, the preview sign-off or the kill switch;
- text from a scraped page, an imported list or a reply that makes Franco
  run tools, follow instructions or put links into an email;
- credentials or personal data written somewhere they shouldn't be.

Out of scope: weaknesses that need someone to already control your machine,
your `.env` or your mailbox, and reports from automated scanners with no
demonstrated impact.

## Running Franco safely

- Keep `.env`, `franco.local.yaml` and the `data/` folder private. They hold
  your credentials and your contacts' personal data, and all three are
  gitignored. See "Your data folder" in the README.
- Don't expose the dashboard beyond localhost.
- Leave approval on (`franco autopilot off`) until you trust the output.
