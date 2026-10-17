# Contributing to Franco

Thanks for helping. Franco is maintained by [Gortex](https://gortex.ai).
Bug reports, fixes, docs and new discovery providers are all welcome.

## Before you start

- **Bugs and ideas:** open an issue using one of the templates. For anything
  larger than a small fix, agree on the approach in an issue before writing
  the code.
- **Security problems:** don't open an issue. See [SECURITY.md](SECURITY.md).
- **Conduct:** this project follows our [Code of Conduct](CODE_OF_CONDUCT.md).

## Set up

```bash
git clone https://github.com/GortexAI/franco.git && cd franco
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The full suite runs offline. It needs no API keys, mailbox or model login.

## Making a change

1. Branch from `main`.
2. Keep the change focused, and match the style of the code around it.
3. Add or update tests. Every behaviour change needs a test that fails
   without it.
4. Run `pytest -q`. CI runs the same suite on Linux, macOS and Windows with
   Python 3.11 and 3.12.
5. Update the README, `CLAUDE.md` and `CHANGELOG.md` (under
   **Unreleased**) when you change behaviour, commands or config.
6. Open a pull request using the template.

Every pull request is checked by tests, CodeQL code scanning, Code Quality and
secret scanning, and needs a maintainer's approval before it can merge.

## Things that must never be weakened

These are the guarantees people rely on when they let Franco send email for
them. A change that loosens one needs an explicit discussion first:

- nothing sends while approval is required, a campaign is in preview, or the
  kill switch is on;
- the pre-send gate, suppression list, compliance footer and blocked claims
  apply to every email;
- text from websites, imported lists and replies is data, never instructions;
- the dashboard only answers its own page on localhost;
- discovery always estimates spend before it spends anything.

## Dependencies

Ranges live in `pyproject.toml` and `requirements.in`. `requirements.txt` is a
generated lockfile. After changing a dependency, regenerate it (the command is
at the top of `requirements.in`). `tests/test_dependencies.py` checks that the
three files agree. Renovate opens dependency updates automatically.

## Releasing

Maintainers cut a release by tagging. Everything else is automated.

1. Rename `## [Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] — YYYY-MM-DD`, and
   start a new empty `## [Unreleased]` above it.
2. Set `version = "X.Y.Z"` in `pyproject.toml`.
3. Merge that through a pull request, then tag the merge commit:
   `git tag vX.Y.Z && git push origin vX.Y.Z`.

The tag triggers two workflows:

| Workflow | What it does |
|---|---|
| `release.yml` | Checks that the tag matches `pyproject.toml` and that the CHANGELOG has notes for it. It then builds the sdist and wheel, runs the full suite and the installed-wheel test, and creates the GitHub Release with those notes, the files attached and provenance attestations. |
| `docker.yml` | Publishes `ghcr.io/gortexai/franco:X.Y.Z`, `:X.Y` and `:latest` for linux/amd64 and linux/arm64, with an SBOM and an attestation. Every push to `main` also publishes `:edge`. |

`release.yml` also publishes the sdist and wheel to
[PyPI](https://pypi.org/project/franco/), before the GitHub Release is created,
using **trusted publishing**: there is no PyPI token anywhere. PyPI accepts the
upload only from `release.yml` in this repository, running in the `pypi`
environment, which only `v*.*.*` tags can deploy to.

One-time setup on pypi.org, by a maintainer: under **Your account →
Publishing**, add a (pending) trusted publisher for project `franco` with owner
`GortexAI`, repository `franco`, workflow `release.yml` and environment `pypi`.

### How the package finds its files

A pip-installed Franco has no checkout. `franco/paths.py` picks a *home
folder* (`$FRANCO_HOME`, else the checkout, else the current folder when it
has a `franco.yaml`, else `~/.franco`), and the wheel bundles the default
prompts, skills, `franco.yaml` and `.env.example` in `franco/_bundled`.
`setup.py` copies them in at build time from the repository's top-level
files, so there is only one copy of each in git. Files are read from the home
folder first and from the bundle otherwise. `tests/test_packaging.py` installs
the wheel into an empty directory and checks all of this, including that
Franco never writes into its installed package.

## Never commit

- `.env`, `franco.local.yaml` or anything in `data/`;
- real prospect lists, researched accounts or email addresses;
- any company's internal sales materials or configuration.

Use `example.com` addresses and made-up companies in tests and docs.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
