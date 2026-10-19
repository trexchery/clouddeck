"""End-to-end checkpoint / restore: the REAL controller drives the REAL lease scripts (restore.sh, checkpoint.sh, agent.sh) inside a sandbox, with a local
bare git repo as the state repo, real gpg, and the Akash API replaced by tests/fake_akash.py.  Linux / WSL only (skipped with the reason elsewhere)."""
import re
import shutil
import tempfile
from pathlib import Path

import pytest

from clouddeck.engine import ddc

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import deleted, git, needs_real_lease, snap  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the lease fixture
pytestmark = needs_real_lease


def alerts():
    return " | ".join(ddc.recent_alerts(50))


def started(lease, *names):
    lease.use(*(names or ("web-app",)))          # one project: fewer archives, a faster checkpoint
    rc, out = lease.start()
    assert rc == 0, out
    lease.box.agent_once()                       # a live box has an agent state: the stop plan and the watchdog read it
    return lease.box


def trees(box):
    return {r["name"]: snap(box.work / r["name"]) for r in ddc.CFG["repos"] + ddc.CFG["dirs"]}


def stop_refused(lease, rc, out, *needles):
    """`ddc stop` refused: non-zero, the dev box is still running, nothing was deleted, the owner was alerted, and the output says why."""
    assert rc == 1, out
    for n in needles:
        assert n in out, f"{n!r} not in:\n{out}"
    assert len(lease.fake.open_dseqs()) == 1 and not deleted(lease.fake)
    assert "stop aborted, dev box left RUNNING" in alerts()


def clone_push(lease, mutate, branch="main", force=False):
    """Change what the state repo holds, the way a stranger, a bug or bit rot could: clone, mutate, push."""
    base = Path(tempfile.mkdtemp(prefix="ddc-mut-"))
    try:
        c = base / "c"
        git("clone", "-q", "file://" + lease.remote.as_posix(), str(c))
        if branch != "main":
            git("checkout", "-q", "-b", branch, cwd=c)
        mutate(c)
        git("add", "-A", cwd=c)
        git("commit", "-qm", "mutated", cwd=c)
        git("push", "-q", *(["-f"] if force else []), "origin", f"{branch}:{branch}", cwd=c)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def set_passphrase(value):
    (ddc.SECRETS_DIR / "secrets.env").write_text(f"AKASH_API_KEY=fake-key\nDDC_BACKUP_PASSPHRASE={value}\n")


# ------------------------------------------------------------------------------------------------ the full round trip
def test_start_work_stop_start_reproduces_head_status_untracked_and_results(lease):
    pc_before = lease.pc.snapshot()
    rc, out = lease.start()
    assert rc == 0, out
    first = lease.box
    # the workspace was seeded from the PC and the controller proved it: same HEAD, same git status
    assert "git status identical to PC" in out and "seeding web-app" in out and "restore: restored= missing=all" in out
    assert first.head("web-app") == git("rev-parse", "HEAD", cwd=lease.pc.web) and first.head("api-server") == git("rev-parse", "HEAD", cwd=lease.pc.api)
    assert (first.work / "web-app" / "notes.txt").read_text() == "untracked on the pc\n" and (first.work / "audit" / "a0.md").exists()
    assert (first.work / ".ddc-restored").exists() and not (first.work / ".ddc" / "restore-started").exists()
    assert all("setup.sh" in c or c.startswith("tmux") for c in lease.fake.skipped), "only setup.sh / tmux / agent restarts are not executed"
    assert "AAAAPUBLIC" not in "".join(c for _, c, _ in lease.fake.inputs), "no key material on a command line"

    before = lease.work_on_lease()
    rc, out = lease.stop()
    assert rc == 0, out
    assert lease.fake.open_dseqs() == [] and ddc.active_deps() == {}
    assert set(lease.heads) == {"main"}
    assert sorted(ddc.STATE_DIR.glob("checkpoints/*/manifest.json")), "the verified copy was kept on this PC"
    assert "verified on this PC" in out and "billing stopped" in out

    rc, out = lease.start()                       # a brand-new lease: new host, empty disk
    assert rc == 0, out
    second = lease.box
    assert second.root != first.root
    assert "restore: restored=" in out and "ws-web-app" in out and "seeding" not in out, "restored from the checkpoint, nothing re-seeded from the PC"
    assert trees(second) == before
    assert (second.work / "web-app" / "lease-only.txt").read_text() == "untracked, created on the lease\n"
    assert (second.work / "results" / "run.log").read_text() == "42 passed\n"
    assert (second.home / ".claude" / "projects" / "p" / "s.jsonl").exists()
    assert lease.pc.snapshot() == pc_before, "the PC checkout was never touched"


def test_a_project_added_after_the_last_checkpoint_is_seeded_from_the_pc_and_restored_ones_are_left_alone(lease):
    started(lease)
    lease.work_on_lease()
    before_web = snap(lease.box.work / "web-app")
    assert lease.stop()[0] == 0
    lease.use("web-app", "api-server")                               # api-server joined the config after that checkpoint
    rc, out = lease.start()
    assert rc == 0, out
    box = lease.box
    assert snap(box.work / "web-app") == before_web, "the restored workspace (with its lease-side work) is not overwritten by a PC re-seed"
    assert "seeding api-server" in out and "seeding web-app" not in out
    assert box.head("api-server") == git("rev-parse", "HEAD", cwd=lease.pc.api)
    assert (box.work / ".ddc" / "complete" / "api-server").exists()


# ------------------------------------------------------------------------------------------------ periodic checkpoints and the agent
def test_periodic_checkpoints_by_the_real_agent_are_skipped_when_unchanged_and_manual_ones_verify(lease):
    box = started(lease)
    assert lease.heads == {}
    st = box.agent_once(DDC_CHECKPOINT_MINUTES="0")                 # the real agent decides it is time and runs the real checkpoint.sh
    assert st["last_checkpoint"] and st["checkpoint_failed"] is False and st["shutdown_requested"] == ""
    first = lease.heads["main"]
    assert set(lease.heads) == {"main"}
    box.agent_once()                                                 # nothing changed since: skipped, the state repo is untouched
    assert lease.heads == {"main": first}
    (box.work / "web-app" / "newfile.txt").write_text("work\n")
    box.agent_once()
    h = lease.heads
    assert h["prev"] == first and h["main"] != first, "a changed workspace gives a new main and rotates the old one to prev"
    rc, out = lease.ddc("checkpoint")                                # `ddc checkpoint`: manual + decrypted and compared on this PC
    assert rc == 0 and "verified on this PC" in out, out
    rc, out = lease.ddc("status")
    assert rc == 0 and "last checkpoint 20" in out and "CHECKPOINT FAILING" not in out, out


def test_checkpoints_that_keep_failing_are_alerted_and_recover_when_the_repo_is_back(lease):
    box = started(lease)
    gone = lease.remote.with_name("gone.git")
    lease.remote.rename(gone)
    st = box.agent_once(DDC_CHECKPOINT_MINUTES="0")
    assert st["checkpoint_failed"] is True and st["last_checkpoint"] == ""
    lease.fake.clock += 60
    assert ddc.main(["watchdog", "--once"]) == 0                     # a first tick only notes the failure
    assert "failing" not in alerts()
    lease.fake.clock += 31 * 60                                       # longer than two checkpoint intervals
    assert ddc.main(["watchdog", "--once"]) == 0
    assert "checkpoints have been failing" in alerts()
    gone.rename(lease.remote)
    st = box.agent_once(DDC_CHECKPOINT_MINUTES="0")
    assert st["checkpoint_failed"] is False and st["last_checkpoint"] and set(lease.heads) == {"main"}


# ------------------------------------------------------------------------------------------------ idle shutdown with the real agent
def test_idle_box_is_stopped_by_the_watchdog_after_a_verified_checkpoint_and_comes_back_intact(lease):
    box = started(lease)
    before = lease.work_on_lease()
    st = box.agent_once(DDC_IDLE_MINUTES="0")                        # the agent: idle -> final-idle checkpoint -> shutdown request
    assert st["shutdown_requested"] == "idle" and st["last_checkpoint"]
    ck1 = lease.heads["main"]
    assert ddc.main(["watchdog", "--once"]) == 0
    assert lease.fake.open_dseqs() == [] and ddc.active_deps() == {}
    h = lease.heads
    assert h["prev"] == ck1 and h["main"] != ck1, "the watchdog's own final-stop checkpoint followed the agent's final-idle one"
    assert "final-stop" in [r for r, _ in lease.fake.checkpoint_runs] and "stop aborted" not in alerts()
    rc, out = lease.start()
    assert rc == 0, out
    assert trees(lease.box) == before


def test_user_returning_before_the_watchdog_acts_withdraws_the_idle_request(lease):
    box = started(lease)
    assert box.agent_once(DDC_IDLE_MINUTES="0")["shutdown_requested"] == "idle"
    main = lease.heads["main"]
    (box.home / ".claude" / "projects" / "p").mkdir(parents=True)
    (box.home / ".claude" / "projects" / "p" / "now.jsonl").write_text("{}\n")            # the user types: a fresh session file
    st = box.agent_once(DDC_IDLE_MINUTES="45")
    assert st["shutdown_requested"] == "" and st["active_reason"] == "claude"
    assert ddc.main(["watchdog", "--once"]) == 0
    assert len(lease.fake.open_dseqs()) == 1 and not deleted(lease.fake) and lease.heads == {"main": main}


# ------------------------------------------------------------------------------------------------ stop refusals (the box stays up)
def test_stop_refuses_a_shrunken_workspace_and_allow_shrink_overrides(lease):
    box = started(lease, "web-app", "audit")
    assert lease.ddc("checkpoint")[0] == 0
    good = lease.heads
    for i in (1, 2, 3):
        (box.work / "audit" / f"a{i}.md").unlink()                   # an accident (or a wiped disk): the workspace lost most of its files
    rc, out = lease.stop()
    stop_refused(lease, rc, out, "refused the checkpoint", "lose ws-audit", "--allow-shrink")
    assert lease.heads == good, "the good checkpoint was not replaced"
    rc, out = lease.stop("--allow-shrink")
    assert rc == 0 and lease.fake.open_dseqs() == [], out
    rc, out = lease.start()
    assert rc == 0 and sorted(p.name for p in (lease.box.work / "audit").iterdir()) == ["a0.md"], "the shrunken workspace is what was saved"


def test_stop_refuses_while_the_state_repo_is_unreachable_and_works_once_it_is_back(lease):
    started(lease)
    assert lease.ddc("checkpoint")[0] == 0
    gone = lease.remote.with_name("gone.git")
    lease.remote.rename(gone)
    rc, out = lease.stop()
    stop_refused(lease, rc, out, "cannot reach the state repo")
    rc, out = lease.ddc("stop", "--dry-run")                         # the plan still works, and says what is known
    assert rc == 0 and "verified first" in out and len(lease.fake.open_dseqs()) == 1
    gone.rename(lease.remote)
    rc, out = lease.stop()
    assert rc == 0 and lease.fake.open_dseqs() == [], out


def test_stop_refuses_when_the_checkpoint_does_not_decrypt_on_this_pc_wrong_passphrase(lease):
    started(lease)
    lease.work_on_lease()
    set_passphrase("not-the-one-on-the-box")
    rc, out = lease.stop()
    stop_refused(lease, rc, out, "cannot decrypt")
    set_passphrase("fake-pass")
    rc, out = lease.stop()
    assert rc == 0 and lease.fake.open_dseqs() == [], out


def test_force_alone_never_destroys_a_workspace_that_could_not_be_verified_force_plus_discard_does_and_says_so(lease):
    started(lease)
    lease.work_on_lease()
    set_passphrase("wrong")
    rc, out = lease.stop("--force")
    assert rc == 1 and "--discard-workspace" in out and len(lease.fake.open_dseqs()) == 1 and not deleted(lease.fake), out
    rc, out = lease.stop("--force", "--discard-workspace")
    assert rc == 0 and lease.fake.open_dseqs() == [] and "AUDIT FORCED CLOSE" in out, out


# ------------------------------------------------------------------------------------------------ restore refusals (the lease stays unrestored, start resumes)
def break_restore(lease, how):
    """Break the restore path after a good checkpoint exists; returns (text `start` must show, undo callable)."""
    if how == "repo-gone":
        gone = lease.remote.with_name("gone.git")
        lease.remote.rename(gone)
        return "cannot reach the state repo", lambda: gone.rename(lease.remote)
    if how == "passphrase":
        set_passphrase("other-pass")
        return "decrypt", lambda: set_passphrase("fake-pass")
    assert how == "truncated"
    good = lease.heads["main"]

    def cut(c):
        p = c / "archives" / "ws-web-app.tar.gz.gpg.part000"
        p.write_bytes(p.read_bytes()[: p.stat().st_size // 2])
    clone_push(lease, cut, force=True)

    def put_back():       # the first lease's state clone still holds the good commit
        src = next(sb for sb in lease.fake.sandboxes.values() if git("cat-file", "-t", good, cwd=sb.sr, check=False) == "commit")
        git("push", "-q", "-f", "file://" + lease.remote.as_posix(), f"{good}:refs/heads/main", cwd=src.sr)
    return "sha256 mismatch", put_back


@pytest.mark.parametrize("how", ["repo-gone", "passphrase", "truncated"])
def test_restore_refusals_leave_the_new_lease_unrestored_and_a_second_start_resumes(lease, how):
    first = started(lease)
    before = lease.work_on_lease()
    assert lease.stop()[0] == 0
    msg, undo = break_restore(lease, how)
    rc, out = lease.start()
    assert rc == 1 and msg in out, out
    assert len(lease.fake.open_dseqs()) == 1, "the lease exists (and bills): the owner is told to act, nothing is hidden"
    box = lease.box
    assert box.root != first.root and not (box.work / ".ddc-restored").exists() and not (box.work / "web-app").exists(), "no half-restored workspace"
    heads_now = None if how == "repo-gone" else lease.heads
    rc, out = lease.stop()                                           # an unrestored lease must never overwrite the good checkpoint
    assert rc == 1 and len(lease.fake.open_dseqs()) == 1 and (heads_now is None or lease.heads == heads_now), out
    undo()
    rc, out = lease.start()                                          # resumes on the SAME lease: no second deployment
    assert rc == 0 and len(lease.fake.deployments) == 2, out
    assert trees(lease.box) == before
    assert lease.stop()[0] == 0 and lease.fake.open_dseqs() == []


@pytest.mark.parametrize("branches", [("feature",), ("main", "feature"), ("main",)], ids=["other-branch-only", "main-and-another-branch", "main-without-manifest"])
def test_a_foreign_state_repo_is_never_written_to_and_never_mistaken_for_a_checkpoint(lease, branches):
    for br in branches:                                              # somebody's real project: not an empty repo, not ours
        clone_push(lease, lambda c, br=br: (c / "README.md").write_text(f"somebody else's project ({br})\n"), branch=br)
    others = lease.heads
    rc, out = lease.use("web-app").start()
    assert rc == 1 and re.search(r"not a ddc (state|checkpoint) repo", out), out
    assert lease.heads == others, "nothing was pushed to it"
    assert not (lease.box.work / ".ddc-restored").exists() and not (lease.box.work / "web-app").exists()
    rc, out = lease.stop()
    assert rc == 1 and len(lease.fake.open_dseqs()) == 1 and lease.heads == others


# ------------------------------------------------------------------------------------------------ checkpoints switched off (empty state_repo)
def test_with_an_empty_state_repo_nothing_is_saved_and_the_workspace_is_never_destroyed_silently(lease, monkeypatch):
    monkeypatch.delenv("DDC_STATE_REMOTE")
    ddc.CFG["state_repo"] = ""
    rc, out = lease.use("web-app").start()
    assert rc == 0 and "checkpoints are disabled" in out and "restored= missing=all" in out, out
    box = lease.box
    assert (box.work / "web-app" / "notes.txt").exists()
    (box.work / "web-app" / "cloud-only.txt").write_text("only exists on the lease\n")
    rc, out = lease.ddc("stop", "--dry-run")
    assert rc == 0 and "CHECKPOINTS DISABLED" in out and "--discard-workspace" in out
    rc, out = lease.ddc("checkpoint")
    assert rc == 1 and "checkpoints are disabled" in out
    rc, out = lease.stop()
    assert rc == 1 and "DESTROY" in out and len(lease.fake.open_dseqs()) == 1 and (box.work / "web-app" / "cloud-only.txt").exists()
    st = box.agent_once(DDC_IDLE_MINUTES="0")                        # the agent still asks for an idle shutdown ...
    assert st["shutdown_requested"] == "idle" and st["last_checkpoint"] == ""
    assert ddc.main(["watchdog", "--once"]) == 0                     # ... the watchdog alerts instead of destroying the workspace
    assert len(lease.fake.open_dseqs()) == 1 and "will not destroy the workspace" in alerts()
    rc, out = lease.stop("--discard-workspace")
    assert rc == 0 and lease.fake.open_dseqs() == [] and lease.heads == {}, out


def test_runtime_limit_closes_a_checkpoint_less_box_because_the_platform_would_anyway(lease, monkeypatch):
    monkeypatch.delenv("DDC_STATE_REMOTE")
    ddc.CFG["state_repo"] = ""
    started(lease)
    lease.box.agent_once()                                           # a fresh state.json for the watchdog to read
    lease.fake.clock += ddc.CFG["max_runtime_hours"] * 3600 - 600    # inside the last 15 minutes of the platform limit
    assert ddc.main(["watchdog", "--once"]) == 0
    assert lease.fake.open_dseqs() == []
