"""Lease side of "empty state_repo = checkpoints disabled": the real lease scripts run in the sandbox of test_lease_scripts.py (WSL/Linux;
skipped with a reason where flock/gpg/bash are missing, e.g. stock Git Bash)."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import test_lease_scripts as lease
from test_lease_scripts import BASH, LEASE, Box, needs_sandbox, px


@pytest.fixture
def make_box():
    made = []

    def make(remote: Path | None = None, disabled: bool = True) -> Box:
        b = Box(remote or Path("/nonexistent/state.git"), "100")
        made.append(b)
        if disabled:  # what the controller writes for an empty state_repo: DDC_STATE_REPO="" and NO test remote
            (b.home / ".config" / "ddc" / "env").write_text('export DDC_STATE_REPO=""\nexport DDC_DSEQ="100"\nexport DDC_REPOS="repoA"\n')
        return b

    yield make
    for b in made:
        b.cleanup()


@needs_sandbox
def test_restore_with_checkpoints_disabled_marks_the_lease_usable_and_reports_nothing_to_restore(make_box):
    b = make_box()
    cp = lease.ok(b.run("restore.sh"), "restore with no state repo")
    assert cp.stdout.strip().splitlines()[-1] == "restored= missing=all"        # the controller then seeds every repo from the PC
    assert (b.work / ".ddc-restored").exists() and not (b.work / ".ddc" / "restore-started").exists() and not b.sr.exists()
    assert (b.work / "results").is_dir()


@needs_sandbox
@pytest.mark.parametrize("reason", ["manual", "periodic", "final-idle", "final-stop", "final-max_runtime"])
def test_checkpoint_with_checkpoints_disabled_is_a_successful_no_op(make_box, reason):
    b = make_box()
    b.seed()
    cp = lease.ok(b.ckpt(reason), reason)
    assert "checkpoint_skip disabled" in cp.stdout and not b.sr.exists() and not (b.run_dir / "last_checkpoint").exists()
    assert not cp.stderr.strip()


@needs_sandbox
def test_checkpoint_still_works_when_only_the_test_remote_is_set(make_box):
    remote = Path(tempfile.mkdtemp(prefix="ddc-remote-")) / "state.git"
    try:
        lease.git("init", "-q", "--bare", "-b", "main", str(remote))
        a = make_box(remote, disabled=False)                                    # DDC_STATE_REPO unset, DDC_STATE_REMOTE set: enabled
        a.seed()
        assert "checkpoint_ok" in lease.ok(a.ckpt("first")).stdout
    finally:
        shutil.rmtree(remote.parent, ignore_errors=True)


@needs_sandbox
def test_agent_does_not_run_checkpoints_when_disabled_but_still_requests_the_idle_shutdown(make_box):
    b = make_box()
    (b.work / "results").mkdir()
    (b.bin / "ps").write_text("#!/bin/sh\nexit 0\n", newline="\n")              # nothing running
    (b.bin / "ps").chmod(0o755)
    cp = subprocess.run([BASH, px(LEASE / "agent.sh")], capture_output=True, text=True, timeout=60,
                        env=b.env({"DDC_AGENT_ONCE": "1", "DDC_CHECKPOINT_MINUTES": "0", "DDC_IDLE_MINUTES": "0"}))
    assert cp.returncode == 0, cp.stderr
    st = json.loads((b.run_dir / "state.json").read_text())
    assert st["checkpoint_failed"] is False and not (b.run_dir / "last_checkpoint").exists()      # no periodic attempt, no failure flag
    assert st["shutdown_requested"] == "idle"          # the PC watchdog gets the request; the controller decides (it refuses to destroy the workspace)
