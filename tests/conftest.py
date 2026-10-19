import shutil
import tempfile
import tomllib
from pathlib import Path

import pytest
from fake_akash import FakeAkash

from clouddeck.engine import ddc

FIXTURE = Path(__file__).parent / "fixtures" / "config.toml"


def _wipe(d: Path) -> None:
    """Empty a state/secrets dir. Refuses anything that is not under the system temp dir (the root conftest.py points both there)."""
    d = d.resolve()
    if Path(tempfile.gettempdir()).resolve() not in d.parents:
        pytest.exit(f"refusing to wipe {d}: not under the temp dir, tests must never touch real state", returncode=2)
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Fresh config, state, ledger, tokens, secrets and keys for every test (no order dependence); no desktop notifications."""
    cfg = tmp_path / "config.toml"
    shutil.copy(FIXTURE, cfg)
    ddc.CONFIG_FILE = cfg
    ddc.CFG.clear()
    ddc.CFG.update(tomllib.loads(cfg.read_text(encoding="utf-8")))
    for d in (ddc.STATE_DIR, ddc.SECRETS_DIR):  # EVERYTHING a previous test left: state, ledger, keys, secrets, logs, verify clones
        _wipe(d)
    monkeypatch.setattr(ddc, "_notify", lambda msg: None)  # alert() must never pop up a real message box / run a real command
    ddc._warned.clear()
    ddc._prov_cache.clear()
    ddc.KEYS.mkdir(parents=True, exist_ok=True)
    (ddc.KEYS / "access.pub").write_text("ssh-ed25519 AAAAPUBLIC access")
    (ddc.KEYS / "worker.pub").write_text("ssh-ed25519 AAAAWORKER worker")
    yield cfg


@pytest.fixture
def jw(monkeypatch, tmp_path):
    """A `ddc job` world on virtual time (tests/job_helpers.py): fake Akash API + fake box + fake clock wired into the controller."""
    from job_helpers import make_world
    return make_world(monkeypatch, tmp_path)


@pytest.fixture
def fake(monkeypatch):
    """The in-memory Akash + lease fake (tests/fake_akash.py) wired into the controller; no repos, guard on."""
    return FakeAkash(ddc, monkeypatch).install()


@pytest.fixture(autouse=True)
def _fake_keys_load(monkeypatch):
    """The fixture keys are placeholder text, not real keys: skip the local ssh-keygen load check (tests/test_controller_followups.py covers it)."""
    monkeypatch.setattr(ddc, "key_problem", lambda key: None)
