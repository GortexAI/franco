"""Franco's home folder and bundled defaults (franco/paths.py), and the
commands that depend on them: `franco init` and `franco install`."""

import argparse
import stat
import sys
from pathlib import Path

import pytest

import franco.paths as paths

REPO = Path(__file__).resolve().parent.parent


# ── which folder is home ──


def test_franco_home_env_wins(tmp_path):
    assert paths.find_home({"FRANCO_HOME": str(tmp_path)}, cwd=Path("/")) == tmp_path.resolve()


def test_a_checkout_is_its_own_home(tmp_path):
    assert paths.checkout_root() == REPO
    assert paths.find_home({}, cwd=tmp_path) == REPO


def test_installed_franco_uses_a_folder_with_config_else_dot_franco(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "checkout_root", lambda: None)
    assert paths.find_home({}, cwd=tmp_path) == paths.DEFAULT_HOME
    (tmp_path / "franco.local.yaml").write_text("persona: {}\n")
    assert paths.find_home({}, cwd=tmp_path) == tmp_path.resolve()
    assert paths.DEFAULT_HOME == Path.home() / ".franco"


def test_site_packages_is_never_mistaken_for_a_checkout(tmp_path, monkeypatch):
    fake_pkg = tmp_path / "lib" / "site-packages" / "franco"
    fake_pkg.mkdir(parents=True)
    (fake_pkg.parent / "prompts").mkdir()           # even with a stray prompts/
    monkeypatch.setattr(paths, "PACKAGE_DIR", fake_pkg)
    assert paths.checkout_root() is None


# ── home copy first, bundled copy otherwise ──


def test_resource_prefers_the_home_copy(tmp_path, monkeypatch):
    bundle = tmp_path / "bundle"
    (bundle / "skills").mkdir(parents=True)
    (bundle / "skills" / "x.md").write_text("bundled")
    monkeypatch.setattr(paths, "BUNDLED_DIR", bundle)
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)

    assert paths.resource("skills", "x.md", home=home) == bundle / "skills" / "x.md"
    (home / "skills" / "x.md").write_text("mine")
    assert paths.resource("skills", "x.md", home=home) == home / "skills" / "x.md"
    # Unknown everywhere: the home path, so errors name where to put it.
    assert paths.resource("skills", "nope.md", home=home) == home / "skills" / "nope.md"


# ── franco init ──


def _init(target):
    from franco.cli import cmd_init
    cmd_init(argparse.Namespace(directory=str(target)))


def test_init_creates_a_complete_private_home(tmp_path, capsys):
    home = tmp_path / "home"
    _init(home)
    assert (home / "franco.yaml").read_text() == (REPO / "franco.yaml").read_text()
    assert (home / ".env").read_text() == (REPO / ".env.example").read_text()
    assert {p.name for p in (home / "prompts").glob("*.md")} == \
        {p.name for p in (REPO / "prompts").glob("*.md")}
    assert (home / "skills" / "email_frameworks.md").is_file()
    assert (home / "data").is_dir()
    gitignore = (home / ".gitignore").read_text()
    assert ".env" in gitignore and "data/" in gitignore and "franco.local.yaml" in gitignore
    if sys.platform != "win32":
        assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    assert "Franco home" in capsys.readouterr().out


def test_init_never_overwrites_and_never_copies_generated_skills(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "skills").mkdir(parents=True)
    (home / "franco.yaml").write_text("mine\n")
    (home / ".env").write_text("SECRET=1\n")
    (home / "skills" / "email_frameworks.md").write_text("edited\n")

    bundle = tmp_path / "bundle"
    (bundle / "skills").mkdir(parents=True)
    (bundle / "prompts").mkdir()
    for name in ("franco.yaml", ".env.example"):
        (bundle / name).write_text("template\n")
    for name in ("email_frameworks.md", "product_knowledge.md", "competitive_intel.md"):
        (bundle / "skills" / name).write_text("bundled\n")
    monkeypatch.setattr(paths, "bundled", lambda kind="": bundle / kind if kind else bundle)

    _init(home)
    _init(home)   # running it twice is harmless
    assert (home / "franco.yaml").read_text() == "mine\n"
    assert (home / ".env").read_text() == "SECRET=1\n"
    assert (home / "skills" / "email_frameworks.md").read_text() == "edited\n"
    assert not (home / "skills" / "product_knowledge.md").exists()
    assert not (home / "skills" / "competitive_intel.md").exists()


# ── franco install ──


def test_install_outside_a_checkout_only_fetches_the_browser(monkeypatch):
    import franco.cli as cli
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(paths, "checkout_root", lambda: None)
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kw: calls.append(args) or Done())
    monkeypatch.setattr(cli, "_check_python_version", lambda: None)
    cli.cmd_install(argparse.Namespace(_project_root="/nowhere"))
    assert calls == [[sys.executable, "-m", "playwright", "install", "chromium"]]
    assert not any("pip" in a for call in calls for a in call)
