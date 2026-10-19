"""Lease-side follow-ups, run on the real scripts in the sandbox of test_lease_scripts.py (WSL/Linux; skipped with a reason where flock/gpg/bash
are missing, e.g. stock Git Bash): restore.sh completion markers for the claude/config archives, the shrink guard for them, and the agent
staying "busy" when it cannot see its own CPU use."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import test_lease_scripts as lease
from test_lease_scripts import BASH, LEASE, Box, needs_sandbox, px


@pytest.fixture
def remote():
    r = Path(tempfile.mkdtemp(prefix="ddc-remote-")) / "state.git"
    lease.git("init", "-q", "--bare", "-b", "main", str(r))
    yield r
    shutil.rmtree(r.parent, ignore_errors=True)


@pytest.fixture
def make_box(remote):
    made = []

    def make(dseq="100") -> Box:
        b = Box(remote, dseq)
        made.append(b)
        return b

    yield make
    for b in made:
        b.cleanup()


def seeded_checkpoint(make_box) -> Box:
    a = make_box("100")
    a.seed()
    for p in ("p1", "p2", "p3"):
        (a.home / ".claude" / "projects" / p).mkdir()
        (a.home / ".claude" / "projects" / p / "s.jsonl").write_text(f'{{"project":"{p}"}}\n')
    lease.ok(a.ckpt("first"), "first checkpoint")
    return a


def cp_shim(box: Box) -> Path:
    """A `cp` that, while the flag file exists, copies only .claude/projects/p1 of the claude archive and dies like a killed process (137)."""
    flag = box.root / "die-in-cp"
    shim = box.bin / "cp"
    shim.write_text('#!/bin/bash\nif [ -e "$SHIM_FLAG" ] && [[ "$2" == */stage/claude/. ]]; then\n'
                    '  mkdir -p "$3/.claude/projects" && /bin/cp -a "${2%/.}/.claude/projects/p1" "$3/.claude/projects/"; exit 137\nfi\nexec /bin/cp "$@"\n', newline="\n")
    shim.chmod(0o755)
    return flag


@needs_sandbox
def test_an_interrupted_claude_copy_is_redone_not_mistaken_for_a_finished_restore(make_box):
    seeded_checkpoint(make_box)
    b = make_box("101")
    flag = cp_shim(b)
    flag.write_text("x")
    extra = {"SHIM_FLAG": px(flag)}
    cp = b.run("restore.sh", extra=extra)
    assert cp.returncode != 0
    projects = b.home / ".claude" / "projects"
    assert sorted(x.name for x in projects.iterdir()) == ["p1"]                     # a partial copy, exactly the verifier's scenario
    assert not (b.work / ".ddc" / "archive-complete" / "claude").exists() and not (b.work / ".ddc-restored").exists()
    assert (b.work / ".ddc" / "restore-started").exists()                           # so checkpoint.sh refuses to run on this box
    assert b.ckpt("final-stop").returncode != 0
    flag.unlink()
    out = lease.ok(b.run("restore.sh", extra=extra), "second restore").stdout.strip().splitlines()[-1]
    assert "claude" in out.split("missing=")[0]                                       # re-copied this time
    assert sorted(x.name for x in projects.iterdir()) == ["p", "p1", "p2", "p3"]
    for n in ("claude", "config"):
        assert (b.work / ".ddc" / "archive-complete" / n).exists()
    assert (b.work / ".ddc-restored").exists() and not (b.work / ".ddc" / "restore-started").exists()
    lease.ok(b.ckpt("final-stop"), "checkpoint after a complete restore")           # and only now may the box checkpoint again


@needs_sandbox
def test_live_claude_data_without_a_marker_is_never_overwritten_by_a_restore(make_box):
    seeded_checkpoint(make_box)
    b = make_box("102")
    live = b.home / ".claude" / "projects" / "p"
    live.mkdir(parents=True)
    (live / "s.jsonl").write_text("LIVE DATA\n")                                    # a lease restored before markers existed (no restore-started)
    lease.ok(b.run("restore.sh"), "restore")
    assert (live / "s.jsonl").read_text() == "LIVE DATA\n"
    assert not (b.home / ".claude" / "projects" / "p1").exists()                    # the archive was skipped entirely, as before


@needs_sandbox
def test_the_shrink_guard_also_covers_the_claude_archive(make_box):
    a = seeded_checkpoint(make_box)
    for p in (a.home / ".claude" / "projects").iterdir():
        shutil.rmtree(p)                                                            # most of the Claude state is gone
    cp = a.ckpt("periodic-test")
    assert cp.returncode != 0 and "checkpoint would lose claude" in cp.stderr
    lease.ok(a.ckpt("manual", "--allow-shrink"), "explicit shrink")


@needs_sandbox
def test_a_box_without_claude_state_can_still_checkpoint(make_box):
    a = seeded_checkpoint(make_box)
    shutil.rmtree(a.home / ".claude")
    (a.home / ".claude.json").unlink(missing_ok=True)                               # not installed here: no claude archive at all
    lease.ok(a.ckpt("manual"), "checkpoint without claude")


def agent_state(b: Box, stat: Path | None) -> dict:
    (b.work / "results").mkdir(exist_ok=True)
    (b.bin / "ps").write_text("#!/bin/sh\necho '1 init /sbin/init'\n", newline="\n")  # nothing busy (an empty listing would be a detection error)
    (b.bin / "ps").chmod(0o755)
    extra = {"DDC_AGENT_ONCE": "1", "DDC_CHECKPOINT_MINUTES": "0", "DDC_IDLE_MINUTES": "0"}
    if stat is not None:
        extra["DDC_CGROUP_CPU_STAT"] = px(stat)
    cp = subprocess.run([BASH, px(LEASE / "agent.sh")], capture_output=True, text=True, timeout=60, env=b.env(extra))
    assert cp.returncode == 0, cp.stderr
    return json.loads((b.run_dir / "state.json").read_text())


@needs_sandbox
def test_the_agent_counts_the_box_as_busy_when_it_cannot_read_its_cpu_use(make_box):
    b = make_box()
    (b.home / ".config" / "ddc" / "env").write_text('export DDC_STATE_REPO=""\nexport DDC_DSEQ="100"\n')
    assert agent_state(b, b.root / "no-such-cpu.stat")["active_reason"] == "cpu_detect_error"
    good = b.root / "cpu.stat"
    good.write_text("usage_usec 1000\nuser_usec 900\n")
    # readable and quiet: no CPU verdict at all (a pts the host happens to have open may still say "tty", so only the cpu reasons are excluded)
    reason = agent_state(b, good)["active_reason"]
    assert reason not in ("cpu_detect_error", "procs_detect_error") and not reason.startswith("cpu=")
