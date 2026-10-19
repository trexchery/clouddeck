"""The controller must refuse to load under pytest unless state/secrets dirs are redirected (a test once clobbered a live state.json)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _import(extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DDC_")}
    env.update(extra_env, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT))
    return subprocess.run([sys.executable, "-c", "import pytest, clouddeck.controller"], env=env, capture_output=True, text=True, cwd=tempfile.gettempdir())


def test_refuses_real_dirs_under_pytest():
    r = _import({})
    assert r.returncode != 0 and "refusing to load the controller under pytest" in r.stderr


def test_loads_when_dirs_are_redirected(tmp_path):
    r = _import({"DDC_STATE_DIR": str(tmp_path / "s"), "DDC_SECRETS_DIR": str(tmp_path / "k")})
    assert r.returncode == 0, r.stderr
