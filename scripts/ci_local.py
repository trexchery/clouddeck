#!/usr/bin/env python3
"""Run the CI gates locally (stdlib only; needs the dev extra, ruff, build and twine installed):

    python scripts/ci_local.py             # ruff, pytest, release_check --no-history, build + twine + check_dist, shellcheck if installed
    python scripts/ci_local.py --release   # the same, but release_check --strict over tree + history (the release.yml gate)
    python scripts/ci_local.py --no-build  # skip the build (faster)

pytest runs with HOME, APPDATA, LOCALAPPDATA, XDG_* and the DDC_* directories redirected to a fresh temp dir (see CONTRIBUTING.md).
Not covered: actionlint, gitleaks, pip-audit (see CONTRIBUTING.md), the Windows/Linux matrix and the wheel smoke test in a clean venv.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--release", action="store_true", help="strict release check over tree + history")
    ap.add_argument("--no-build", action="store_true", help="skip building and checking the sdist and wheel")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="clouddeck-ci-"))
    home = tmp / "home"
    for d in ("home", "local", "roaming", "state", "secrets", "xdg"):
        (tmp / d).mkdir()
    test_env = os.environ | {
        "HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(tmp / "local"), "APPDATA": str(tmp / "roaming"),
        "XDG_CONFIG_HOME": str(tmp / "xdg" / "config"), "XDG_STATE_HOME": str(tmp / "xdg" / "state"), "XDG_DATA_HOME": str(tmp / "xdg" / "data"),
        "DDC_STATE_DIR": str(tmp / "state"), "DDC_SECRETS_DIR": str(tmp / "secrets"), "PYTHONDONTWRITEBYTECODE": "1",
    }  # fmt: skip
    xdist = ["-n", "auto"] if importlib.util.find_spec("xdist") else []
    steps: list[tuple[str, list[str], dict | None]] = [
        ("ruff check", [PY, "-m", "ruff", "check", "."], None),
        ("ruff format (enforced subset)", [PY, "-m", "ruff", "format", "--check", "scripts", "tests/test_release_check.py"], None),
        ("pytest", [PY, "-m", "pytest", "-p", "no:cacheprovider", "-q", *xdist], test_env),
        ("release_check", [PY, "scripts/release_check.py", *(["--strict"] if args.release else ["--no-history"])], None),
    ]
    if not args.no_build:
        dist = tmp / "dist"
        steps += [
            ("build", [PY, "-m", "build", "--outdir", str(dist)], None),
            ("twine check", [PY, "-m", "twine", "check", "--strict", str(dist / "*")], None),
            ("check_dist", [PY, "scripts/check_dist.py", str(dist / "*.whl"), str(dist / "*.tar.gz")], None),
        ]
    if shutil.which("shellcheck"):
        lease, jobs = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "lease").glob("*.sh")), sorted(str(p.relative_to(ROOT)) for p in (ROOT / "jobremote").glob("*.sh"))
        steps += [
            ("shellcheck lease", ["shellcheck", "-x", "-S", "warning", "-P", "lease", *lease], None),
            ("shellcheck jobremote + bin", ["shellcheck", "-S", "warning", *jobs, "bin/clouddeck"], None),
        ]
    else:
        print("note: shellcheck not found, skipped (pip install shellcheck-py)")
    failed = []
    for name, cmd, env in steps:
        print(f"\n==> {name}", flush=True)
        if name in ("twine check", "check_dist"):  # expand the dist globs here (no shell on Windows)
            cmd = [c2 for c in cmd for c2 in (sorted(map(str, Path(c).parent.glob(Path(c).name))) if "*" in c else [c])]
        if subprocess.run(cmd, cwd=ROOT, env=env).returncode:
            failed.append(name)
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nci_local: " + (f"FAILED: {', '.join(failed)}" if failed else "all gates passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
