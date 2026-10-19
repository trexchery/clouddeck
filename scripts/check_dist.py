#!/usr/bin/env python3
"""Check a built wheel (.whl) or sdist (.tar.gz): the runtime data files are inside and no dev/private files leaked in.

    python scripts/check_dist.py dist/clouddeck-0.1.0-py3-none-any.whl dist/clouddeck-0.1.0.tar.gz

The expected lease/ and jobremote/ files are read from the repo's own directories, so adding a script there needs no change here.  Stdlib only.
"""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ["sdl.yaml.tmpl", "config.example.toml"]
LEASE = sorted(p.name for p in (ROOT / "lease").iterdir() if p.is_file())
JOBREMOTE = sorted(p.name for p in (ROOT / "jobremote").iterdir() if p.is_file())  # scripts `ddc job` uploads to / inlines for a job's box
JOBPKG = sorted(p.name for p in (ROOT / "clouddeck" / "job").glob("*.py"))
EXAMPLES = sorted(p.name for p in (ROOT / "examples").glob("*.toml"))  # in the sdist and the repository, NOT in the wheel (the docs say so)
WHEEL_NEED = [
    "clouddeck/controller.py",
    "clouddeck/cli.py",
    "clouddeck/tui/clouddeck.tcss",
    *(f"clouddeck/data/{n}" for n in DATA),
    *(f"clouddeck/data/lease/{n}" for n in LEASE),
    *(f"clouddeck/data/jobremote/{n}" for n in JOBREMOTE),
    *(f"clouddeck/job/{n}" for n in JOBPKG),
]
SDIST_NEED = [
    "pyproject.toml",
    "README.md",
    "LICENSE",
    *DATA,
    *(f"lease/{n}" for n in LEASE),
    *(f"jobremote/{n}" for n in JOBREMOTE),
    *(f"clouddeck/job/{n}" for n in JOBPKG),
    *(f"examples/{n}" for n in EXAMPLES),
    "docker/Dockerfile",
    "docs/JOBS.md",
]
# never in an artifact: repo-root dev shim, tests, CI config, secrets-looking files
WHEEL_BAN = ("ddc.py", "conftest.py", "test_ddc.py", "tests/", "examples/", ".github/", ".env", "secrets.env", "keys/", "state.json")


def names(path: Path) -> list[str]:
    if path.suffix == ".whl":
        return zipfile.ZipFile(path).namelist()
    with tarfile.open(path) as t:
        # sdist members are prefixed with "<name>-<version>/"
        return [n.split("/", 1)[1] for n in t.getnames() if "/" in n]


def check(path: Path) -> list[str]:
    have = set(names(path))
    problems = []
    if path.suffix == ".whl":
        problems += [f"missing {n}" for n in WHEEL_NEED if n not in have]
        problems += [f"unexpected {n}" for n in sorted(have) if n.startswith(WHEEL_BAN)]
        if not any(n.endswith(".dist-info/entry_points.txt") for n in have):
            problems.append("missing entry_points.txt")
        if not any(n.endswith(".dist-info/licenses/LICENSE") for n in have):
            problems.append("missing LICENSE in dist-info (set license-files in pyproject)")
    else:
        problems += [f"missing {n}" for n in SDIST_NEED if n not in have]
        problems += [f"unexpected {n}" for n in sorted(have) if n.startswith((".env", "secrets.env", "keys/", "state.json")) and n != ".env.example"]
    problems += [f"private file {n}" for n in sorted(have) if "release_denylist" in n]  # the maintainer's local denylist never ships
    return problems


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    bad = 0
    for a in argv:
        problems = check(Path(a))
        print(f"{a}: {'ok' if not problems else str(len(problems)) + ' problem(s)'}")
        for p in problems:
            print(f"  {p}")
        bad += bool(problems)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
