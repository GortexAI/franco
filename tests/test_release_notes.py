"""scripts/release_notes.py: the notes a GitHub Release is created with."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts" / "release_notes.py")
rn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rn)

TEXT = """# Changelog

## [Unreleased]

- next thing

## [0.4.0] — 2026-10-20

### Added
- the feature

## [0.3.0] — 2026-10-13

- older
"""


def test_extracts_one_version_section_only():
    notes = rn.section(TEXT, "0.4.0")
    assert notes.startswith("### Added") and "the feature" in notes
    assert "older" not in notes and "next thing" not in notes


def test_last_section_runs_to_the_end():
    assert rn.section(TEXT, "0.3.0") == "- older"


def test_missing_version_fails_the_release(capsys):
    assert rn.section(TEXT, "9.9.9") == ""
    assert rn.main(["release_notes.py", "v9.9.9"]) == 1
    assert "no notes for 9.9.9" in capsys.readouterr().err


def test_real_changelog_has_notes_for_the_current_version():
    import tomllib
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert rn.section((ROOT / "CHANGELOG.md").read_text(), version)
