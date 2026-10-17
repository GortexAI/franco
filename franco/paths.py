"""Where Franco keeps its files.

Franco works in one folder, its *home*: config (franco.yaml,
franco.local.yaml, .env), editable knowledge (prompts/, skills/) and state
(data/). The home is, in order:

1. ``$FRANCO_HOME``, when set;
2. the source checkout this package lives in (a git clone, an editable
   install, or the Docker image's /app). That is the repo-centric layout
   Franco has always had;
3. the current directory, when it holds franco.yaml or franco.local.yaml
   (a folder made with ``franco init``);
4. ``~/.franco`` otherwise.

A pip-installed Franco never writes into site-packages: without a checkout
it uses 3 or 4. Prompts, skills and the config template also ship inside
the package (franco/_bundled, copied in at build time), so a home only needs
the files you want to change: ``resource()`` reads the home's copy first
and falls back to the bundled one.

``resolve()`` matters: when the package is reached through a symlink (some
editable installs), a bare ``Path(__file__).parent`` would point into
site-packages and Franco would silently read/write the wrong files.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
BUNDLED_DIR = PACKAGE_DIR / "_bundled"
CONFIG_NAMES = ("franco.local.yaml", "franco.yaml")
DEFAULT_HOME = Path.home() / ".franco"


def checkout_root() -> Path | None:
    """The source tree around this package, if there is one.

    A checkout has prompts/ next to the package. An installed wheel never
    does (site-packages has no prompts/), so this can't misfire there.
    """
    root = PACKAGE_DIR.parent
    if (root / "prompts").is_dir() and "site-packages" not in root.parts \
            and "dist-packages" not in root.parts:
        return root
    return None


def find_home(env: dict | None = None, cwd: Path | None = None) -> Path:
    """Franco's home folder (see the module docstring for the order)."""
    env = os.environ if env is None else env
    explicit = (env.get("FRANCO_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    checkout = checkout_root()
    if checkout is not None:
        return checkout
    cwd = (cwd or Path.cwd()).resolve()
    if any((cwd / name).is_file() for name in CONFIG_NAMES):
        return cwd
    return DEFAULT_HOME


# Kept under its historical name: every module joins paths onto it.
PROJECT_ROOT = find_home()


def resource(kind: str, name: str, home: Path | None = None) -> Path:
    """A prompt, skill or template: the home's copy if it has one, else the
    copy bundled with the package. ``kind`` is "prompts", "skills" or ""
    (a top-level file such as "franco.yaml")."""
    home = PROJECT_ROOT if home is None else home
    local = home / kind / name if kind else home / name
    if local.is_file():
        return local
    bundled = BUNDLED_DIR / kind / name if kind else BUNDLED_DIR / name
    return bundled if bundled.is_file() else local


def bundled(kind: str = "") -> Path:
    """The bundled defaults (a checkout serves its own files)."""
    if BUNDLED_DIR.is_dir():
        return BUNDLED_DIR / kind if kind else BUNDLED_DIR
    root = checkout_root() or PACKAGE_DIR.parent
    return root / kind if kind else root
