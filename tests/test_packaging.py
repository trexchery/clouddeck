"""Wheel layout: runtime data ships inside the package and the installed `ddc` runs with the checkout NOT on sys.path.
Opt-in (builds in a temp copy, needs pip + network for the build backend):  CLOUDDECK_TEST_WHEEL=1 pytest tests/test_packaging.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not os.environ.get("CLOUDDECK_TEST_WHEEL"), reason="set CLOUDDECK_TEST_WHEEL=1 to build a wheel")


def run(*argv, cwd, env=None):
    cp = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True, env=env, timeout=600)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout


def test_built_wheel_ships_data_and_runs_from_a_clean_venv():
    with tempfile.TemporaryDirectory(prefix="clouddeck-wheel-") as td:
        td = Path(td)
        src = td / "src"
        shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns(".git", "__pycache__", "build", "dist", "*.egg-info", ".pytest_cache"))
        run(sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", td / "wheel", cwd=src)
        whl = next((td / "wheel").glob("clouddeck-*.whl"))
        names = zipfile.ZipFile(whl).namelist()
        assert "clouddeck/controller.py" in names and "ddc.py" not in names
        assert {"clouddeck/data/sdl.yaml.tmpl", "clouddeck/data/config.example.toml", "clouddeck/data/lease/setup.sh"} <= set(names)
        assert {"clouddeck/data/jobremote/boot.sh", "clouddeck/data/jobremote/step.sh", "clouddeck/data/jobremote/fetch.sh", "clouddeck/job/runner.py", "clouddeck/job/cli.py"} <= set(names)
        assert not any(n.startswith(("ddcjob/", "examples/", "docker/")) for n in names)       # the job package lives inside clouddeck/; no stray top-level package
        run(sys.executable, str(ROOT / "scripts" / "check_dist.py"), whl, cwd=td)               # every file the repo ships is in the wheel
        venv = td / "venv"
        run(sys.executable, "-m", "venv", venv, cwd=td)
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        run(bindir / "python", "-m", "pip", "install", "--no-deps", whl, cwd=td)
        home = td / "home"
        home.mkdir()
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home / "ad"), "LOCALAPPDATA": str(home / "la"),
               "XDG_CONFIG_HOME": str(home / "xc"), "XDG_STATE_HOME": str(home / "xs")}
        for k in ("DDC_CONFIG", "DDC_STATE_DIR", "DDC_SECRETS_DIR", "PYTHONPATH"):
            env.pop(k, None)
        exe = ".exe" if os.name == "nt" else ""
        assert "Disposable Dev Cloud" in run(bindir / f"ddc{exe}", "--help", cwd=home, env=env)       # no config, repo not on sys.path
        out = run(bindir / "python", "-I", "-c", "import clouddeck.controller as c; print(c.data_file('lease').is_dir(), c.CHECKOUT, c.data_file('jobremote').is_dir())", cwd=home, env=env)
        assert out.split()[0] == "True" and out.split()[1] == "None" and out.split()[2] == "True"
        assert "close JOB" in run(bindir / "python", "-I", "-m", "clouddeck.job", "--help", cwd=home, env=env)   # `python -m clouddeck.job` works from the wheel
