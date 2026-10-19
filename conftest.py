"""Test isolation: the controller (clouddeck.controller) reads its config/state/secrets paths from the environment at import time, so this runs before
anything imports it.  Tests can therefore never read or write the real config, state, ledger or keys."""
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="clouddeck-test-"))
(ROOT / "state").mkdir()
(ROOT / "secrets" / "keys").mkdir(parents=True)
shutil.copy(Path(__file__).parent / "tests" / "fixtures" / "config.toml", ROOT / "config.toml")
os.environ["DDC_STATE_DIR"] = str(ROOT / "state")
os.environ["DDC_SECRETS_DIR"] = str(ROOT / "secrets")
os.environ["DDC_CONFIG"] = str(ROOT / "config.toml")
os.environ["CLOUDDECK_DEMO_LATENCY"] = "0"
(ROOT / "secrets" / "keys" / "access.pub").write_text("ssh-ed25519 AAAAPUBLIC access")
(ROOT / "secrets" / "keys" / "worker.pub").write_text("ssh-ed25519 AAAAWORKER worker")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(ROOT, ignore_errors=True)
