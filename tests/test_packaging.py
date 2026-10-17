"""Opt-in: build the wheel from a copy of the sources, install it into an
empty target, and use it with no working-tree imports.

    FRANCO_PACKAGE_TESTS=1 pytest tests/test_packaging.py

Needs setuptools in the running environment (no network: the build runs
without isolation). Covers the CLI entry point, the version preflight, a
Brain call through the installed Codex adapter to a scripted `codex` found
on PATH (a real executable, no monkeypatching), and the dashboard assets.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).with_name("fake_codex.py")

pytestmark = pytest.mark.skipif(
    os.environ.get("FRANCO_PACKAGE_TESTS") != "1" or sys.platform == "win32",
    reason="set FRANCO_PACKAGE_TESTS=1 (needs setuptools); Codex runs on macOS/Linux only",
)

CHECK = textwrap.dedent('''
    import asyncio, base64, contextlib, io, json, os, sys, tempfile, time
    from datetime import datetime, timezone
    SITE, FAKE, WORK = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, SITE)
    # An editable install of the checkout must not shadow the wheel.
    sys.meta_path[:] = [f for f in sys.meta_path
                        if "editable" not in type(f).__name__.lower()
                        and "editable" not in getattr(f, "__module__", "")]
    import franco, franco.codex as hc
    from franco.brain import Brain
    from franco.config import BrainConfig
    from franco.state import StateManager
    assert os.path.realpath(franco.__file__).startswith(os.path.realpath(SITE)), franco.__file__

    def cli(*argv):
        from franco.cli import main
        out = io.StringIO()
        sys.argv = ["franco", *argv]
        with contextlib.redirect_stdout(out):
            try:
                main()
            except SystemExit:
                pass
        return out.getvalue()

    assert "train" in cli("--help")
    train = cli("train", "--help")
    assert "--provider" in train and "--model" in train

    bindir = os.path.join(WORK, "bin dir ü")
    os.makedirs(bindir)
    if os.name == "nt":
        with open(os.path.join(bindir, "codex.cmd"), "w") as f:
            f.write(f'@"{sys.executable}" "{FAKE}" smart "{WORK}\\\\fake.log" %*\\n')
    else:
        path = os.path.join(bindir, "codex")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\\nexec "{sys.executable}" "{FAKE}" smart "{WORK}/fake.log" "$@"\\n')
        os.chmod(path, 0o755)
    os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
    native = os.path.join(WORK, "native")
    os.makedirs(native)
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    jwt = lambda c: f"{enc({'alg': 'none'})}.{enc(c)}.sig"
    auth = os.path.join(native, "auth.json")
    with open(auth, "w") as f:
        json.dump({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
            "id_token": jwt({"email": "x@example.invalid"}),
            "access_token": jwt({"exp": time.time() + 86400}),
            "refresh_token": "fake-refresh", "account_id": "a"},
            "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}, f)
    os.chmod(auth, 0o600)
    os.environ["CODEX_HOME"] = native
    tempfile.tempdir = os.path.join(WORK, "tmp")
    os.makedirs(tempfile.tempdir)

    assert hc.check_cli_version() == "0.159.3"

    async def main():
        state = StateManager(os.path.join(WORK, "franco.db"))
        await state.init_db()
        brain = Brain(state, BrainConfig(provider="codex", model="gpt-5.5"))
        assert await brain.preflight() == ""
        answer = await brain.think("Classify this email reply: could you share pricing?")
        assert answer == "interested", answer
        assert await state.get_usage_today("codex") == 1

    asyncio.run(main())
    assert os.listdir(tempfile.tempdir) == []
    from franco.dashboard import app  # noqa: F401
    web = os.path.join(os.path.dirname(franco.__file__), "web")
    assert os.path.exists(os.path.join(web, "app.js"))
    print("WHEEL-OK")
''')


def _build_and_install(tmp_path):
    """Build the wheel from a copy of exactly what the sdist ships, and
    install it into an empty --target directory."""
    pytest.importorskip("setuptools")
    src = tmp_path / "src"
    src.mkdir()
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md", "LICENSE",
                 "franco.yaml", ".env.example"):
        shutil.copy(ROOT / name, src / name)
    for folder in ("franco", "prompts", "skills"):
        shutil.copytree(ROOT / folder, src / folder,
                        ignore=shutil.ignore_patterns("__pycache__", "product_knowledge.md",
                                                      "competitive_intel.md"))
    dist, site = tmp_path / "dist", tmp_path / "site"
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps",
                    "--no-build-isolation", "-w", str(dist), str(src)],
                   check=True, cwd=tmp_path, timeout=300)
    wheel = next(dist.glob("franco-*.whl"))
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-deps",
                    "--target", str(site), str(wheel)], check=True, timeout=300)
    return site


def test_installed_wheel_runs_codex_without_the_checkout(tmp_path):
    site = _build_and_install(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    script = tmp_path / "check.py"
    script.write_text(CHECK)
    out = subprocess.run([sys.executable, "-I", str(script), str(site), str(FAKE), str(work)],
                         cwd=work, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert "WHEEL-OK" in out.stdout


# pip-installed Franco: `franco init` makes a home folder, Franco finds it by
# its franco.yaml, reads the home's own prompts first and the bundled ones
# otherwise, keeps its data there, and never writes into site-packages.
PREAMBLE = textwrap.dedent('''
    import os, sys
    SITE = sys.argv[1]
    sys.path.insert(0, SITE)
    sys.meta_path[:] = [f for f in sys.meta_path
                        if "editable" not in type(f).__name__.lower()
                        and "editable" not in getattr(f, "__module__", "")]
''')

INIT = PREAMBLE + textwrap.dedent('''
    from franco.cli import main
    sys.argv = ["franco", "init", sys.argv[2]]
    main()
''')

USE_HOME = PREAMBLE + textwrap.dedent('''
    import asyncio
    HOME = os.path.realpath(sys.argv[2])
    import franco
    from franco import paths
    assert os.path.realpath(franco.__file__).startswith(os.path.realpath(SITE)), franco.__file__
    assert paths.checkout_root() is None, paths.checkout_root()
    assert str(paths.PROJECT_ROOT) == HOME, (paths.PROJECT_ROOT, HOME)

    from franco.config import load_config
    assert load_config().outreach.mode == "generated"

    # The home's own copy wins; a deleted one falls back to the bundled copy.
    assert str(paths.resource("prompts", "writer.md")).startswith(HOME)
    os.remove(os.path.join(HOME, "skills", "signal_playbook.md"))
    fallback = str(paths.resource("skills", "signal_playbook.md"))
    assert fallback.startswith(os.path.realpath(SITE)) and "_bundled" in fallback, fallback
    from franco.brain import Brain
    assert len(Brain.__new__(Brain).load_skill("signal_playbook")) > 200

    from franco.state import StateManager, DB_PATH
    assert str(DB_PATH).startswith(HOME), DB_PATH
    asyncio.run(StateManager().init_db())
    assert os.path.isfile(os.path.join(HOME, "data", "franco.db"))

    from fastapi.testclient import TestClient
    import franco.dashboard as dash
    c = TestClient(dash.app, base_url="http://localhost")
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/api/outbox").status_code == 200
    print("HOME-OK")
''')


def _snapshot(folder):
    return {p: p.read_bytes() for p in folder.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def test_installed_wheel_works_from_a_franco_init_home(tmp_path):
    pytest.importorskip("fastapi")
    site = _build_and_install(tmp_path)
    assert (site / "franco" / "_bundled" / "prompts" / "writer.md").is_file()
    assert (site / "franco" / "_bundled" / "franco.yaml").is_file()
    assert not (site / "franco" / "_bundled" / "skills" / "product_knowledge.md").exists()
    before = _snapshot(site)

    home = tmp_path / "my franco home"
    init = tmp_path / "init.py"
    init.write_text(INIT)
    env = {k: v for k, v in os.environ.items() if k != "FRANCO_HOME"}
    out = subprocess.run([sys.executable, "-I", str(init), str(site), str(home)],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert (home / "franco.yaml").is_file() and (home / ".env").is_file()
    assert (home / ".env").stat().st_mode & 0o077 == 0, "the .env must be private"

    use = tmp_path / "use.py"
    use.write_text(USE_HOME)
    out = subprocess.run([sys.executable, "-I", str(use), str(site), str(home)],
                         cwd=home, env=env, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr[-3000:]
    assert "HOME-OK" in out.stdout
    assert _snapshot(site) == before, "Franco wrote into its installed package"
