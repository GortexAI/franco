"""Build hook: bundle Franco's defaults inside the package.

pyproject.toml holds all the metadata; this file only extends ``build_py``
so that wheels (and installs from the sdist) carry the default prompts,
skills, config template and .env template in ``franco/_bundled``. A
pip-installed Franco reads them from there when its home folder has no copy
of its own (see franco/paths.py).

The repository keeps these files at the top level, where people edit them,
so there is exactly one copy of each in git.
"""

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent

# What ships, by explicit allowlist: product_knowledge.md and
# competitive_intel.md are generated per product by `franco train` and must
# never be packaged, even when a maintainer builds from their own checkout.
BUNDLE_FILES = ("franco.yaml", ".env.example")
BUNDLE_DIRS = ("prompts", "skills")
NEVER_SHIP = {"product_knowledge.md", "competitive_intel.md"}


class BuildWithDefaults(build_py):
    def run(self):
        super().run()
        dest = Path(self.build_lib) / "franco" / "_bundled"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for name in BUNDLE_FILES:
            shutil.copyfile(ROOT / name, dest / name)
        for folder in BUNDLE_DIRS:
            (dest / folder).mkdir()
            for src in sorted((ROOT / folder).glob("*.md")):
                if src.name not in NEVER_SHIP:
                    shutil.copyfile(src, dest / folder / src.name)


setup(cmdclass={"build_py": BuildWithDefaults})
