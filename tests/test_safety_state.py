"""State and ledger integrity: atomic writes with a backup, corrupt files fail CLOSED (never read as "no leases"), a torn ledger tail is
tolerated, cross-process locking, and refresh_live never reads an empty/404/error answer as "closed"."""
import json
import os
import subprocess
import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest

from clouddeck.engine import ddc

ROOT = Path(ddc.ROOT)


def lease(role="worker", status="ready", **kw):
    return {"ours": True, "name": f"ddc-{role}-32-x", "role": role, "status": status, "usd_hr": 0.3, "lease_at": ddc.now(), "limit_h": 4,
            "host": "h.example", "port": 22, "provider": "p", "idx": 1, **kw}


def bak():
    return ddc.STATE_FILE.with_name("state.json.bak")


# ------------------------------------------------------------------ atomic writes + backup
def test_state_writes_are_atomic_and_keep_the_previous_good_copy():
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease()}})
    assert not bak().exists()
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease(), "2": lease()}})
    assert set(json.loads(bak().read_text())["deps"]) == {"1"} and set(ddc.load_state()["deps"]) == {"1", "2"}
    assert not ddc.STATE_FILE.with_name("state.json.tmp").exists()


def test_a_crash_between_the_temp_file_and_the_replace_leaves_the_old_state_intact(monkeypatch):
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease()}})
    monkeypatch.setattr(ddc.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("power loss")))
    with pytest.raises(OSError):
        ddc.update_dep("2", ours=True, name="ddc-x")
    monkeypatch.undo()
    assert set(ddc.load_state()["deps"]) == {"1"}                  # never a half-written state.json


def test_a_damaged_current_file_never_overwrites_the_good_backup():
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease()}})
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease(), "2": lease()}})
    good = bak().read_text()
    ddc.STATE_FILE.write_text('{"deps": {"1"', encoding="utf-8")
    ddc._backup_state()
    assert bak().read_text() == good


def test_existing_state_files_keep_loading_and_flag_only_files_are_fine():
    ddc.STATE_FILE.write_text(json.dumps({"deps": {"9": {"ours": True, "name": "ddc-dev-32-old", "status": "ready"}}, "alerted_80": True, "idle_hold_until": 5}))
    assert ddc.load_state()["deps"]["9"]["name"] == "ddc-dev-32-old" and ddc.load_state()["alerted_80"] is True
    ddc.STATE_FILE.write_text(json.dumps({"idle_hold_until": 5}))                 # no leases key at all: still valid state
    assert ddc.load_state() == {"idle_hold_until": 5, "deps": {}}


# ------------------------------------------------------------------ corrupt state fails closed
@pytest.mark.parametrize("text", ['{"deps": {"1": {"ours": tr', "", "[]", '"x"', '{"deps": []}', '{"deps": {"1": "str"}}', "\x00\x00\x00"])
def test_corrupt_state_is_quarantined_and_blocks_new_leases_but_is_never_read_as_empty(fake, text):
    ddc.STATE_FILE.write_text(text, encoding="utf-8")
    with pytest.raises(ddc.StateCorrupt, match="will NOT create leases"):
        ddc.load_state()
    moved = list(ddc.STATE_DIR.glob("state.json.corrupt-*"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == text and not ddc.STATE_FILE.exists()   # moved aside, never deleted
    assert (ddc.STATE_DIR / "state.corrupt").exists()
    for call in (lambda: ddc.active_deps(), lambda: ddc.create_box("dev", "32"), lambda: ddc.check_budget("32", 1, 8), lambda: ddc.update_dep("1", a=1)):
        with pytest.raises(ddc.StateCorrupt):                                     # the file is gone now, still not "no leases"
            call()
    assert not fake.calls and not ddc.STATE_FILE.exists()                          # no API call, nothing re-created


def test_a_deleted_state_file_with_running_leases_in_the_backup_is_flagged():
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease()}})
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": lease(), "2": lease()}})
    ddc.STATE_FILE.unlink()
    with pytest.raises(ddc.StateCorrupt, match="missing but state.json.bak lists running leases"):
        ddc.load_state()
    ddc.STATE_DIR.joinpath("state.corrupt").unlink()
    bak().write_text(json.dumps({"deps": {"1": {**lease(), "status": "closed"}}}))      # only closed leases in the backup: a fresh start
    assert ddc.load_state() == {"deps": {}}


def test_status_still_works_and_shows_the_problem_and_the_account(fake, capsys):
    foreign = fake.add_foreign()
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    ddc.STATE_FILE.write_text("{ broken", encoding="utf-8")
    assert ddc.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "STATE PROBLEM" in out and "ddc reconcile" in out and box["dseq"] in out and f"{foreign}" in out and "(not ours)" in out


def test_reconcile_restores_the_backup_or_adopts_from_the_account_and_stop_can_then_close_it(fake, capsys):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    ddc.update_dep(box["dseq"], note="second write so a .bak exists")
    ddc.STATE_FILE.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ddc.StateCorrupt):
        ddc.load_state()
    assert ddc.main(["reconcile"]) == 0                                            # report only
    out = capsys.readouterr().out
    assert box["dseq"] in out and "Nothing was changed" in out and (ddc.STATE_DIR / "state.corrupt").exists() and fake.open_dseqs() == [box["dseq"]]
    assert ddc.main(["reconcile", "--restore-backup"]) == 0
    assert not (ddc.STATE_DIR / "state.corrupt").exists() and ddc.load_state()["deps"][box["dseq"]]["ours"]
    # no usable backup: rebuild from the account (only ddc-* names are adopted, a foreign lease never is)
    fake.add_foreign()
    ddc.STATE_FILE.write_text("{ broken", encoding="utf-8")
    bak().unlink()
    with pytest.raises(ddc.StateCorrupt):
        ddc.load_state()
    assert ddc.main(["reconcile", "--restore-backup"]) == 1
    assert ddc.main(["reconcile", "--adopt", "--yes"]) == 0
    st = ddc.load_state()
    assert list(st["deps"]) == [box["dseq"]] and st["deps"][box["dseq"]]["ours"] and st["deps"][box["dseq"]]["role"] == "worker"
    assert st["deps"][box["dseq"]]["adopted"] and ddc.valid_host(st["deps"][box["dseq"]]["host"]) and not (ddc.STATE_DIR / "state.corrupt").exists()
    assert ddc.main(["stop", "--only", box["dseq"], "--yes"]) == 0 and fake.open_dseqs() == ["1000000000001"]


def test_adopt_with_nothing_on_the_account_clears_the_flag(fake):
    ddc.STATE_FILE.write_text("", encoding="utf-8")
    with pytest.raises(ddc.StateCorrupt):
        ddc.load_state()
    assert ddc.main(["reconcile", "--adopt", "--yes"]) == 0 and ddc.load_state() == {"deps": {}}


# ------------------------------------------------------------------ ledger
def test_a_torn_final_ledger_line_is_tolerated_and_repaired_on_the_next_append():
    ddc.ledger_add({"event": "closed", "dseq": "1", "cost_usd": 1.5})
    with ddc.LEDGER.open("ab") as f:
        f.write(b'{"at": "2026-10-17T10:00:00+00:00", "event": "clo')              # power loss mid-append
    assert ddc.ledger_spent() == 1.5 and ddc.ledger_read()[1] == []                # not an error, not "no spend"
    ddc.ledger_add({"event": "closed", "dseq": "2", "cost_usd": 2.0})
    entries, bad = ddc.ledger_read()
    assert bad == [] and ddc.ledger_spent() == 3.5 and len(entries) == 2
    assert len(list(ddc.STATE_DIR.glob("ledger.jsonl.torn-*"))) == 1                # the fragment is kept aside
    assert ddc.LEDGER.read_bytes().endswith(b"\n")


def test_a_complete_final_line_missing_only_its_newline_is_kept():
    ddc.ledger_add({"event": "closed", "dseq": "1", "cost_usd": 1.0})
    ddc.LEDGER.write_bytes(ddc.LEDGER.read_bytes().rstrip(b"\n"))
    assert ddc.ledger_spent() == 1.0
    ddc.ledger_add({"event": "closed", "dseq": "2", "cost_usd": 1.0})
    assert ddc.ledger_spent() == 2.0 and not list(ddc.STATE_DIR.glob("ledger.jsonl.torn-*"))


def test_a_corrupt_middle_line_blocks_new_leases_but_not_status_or_the_watchdog(fake, capsys):
    ddc.ledger_add({"event": "closed", "dseq": "1", "cost_usd": 4.0})
    lines = ddc.LEDGER.read_text().splitlines()
    ddc.LEDGER.write_text("\n".join([lines[0], "NOT JSON", json.dumps({"event": "closed", "cost_usd": 1.0})]) + "\n")
    assert ddc.ledger_read()[1] == [2] and ddc.ledger_spent() == 5.0                # skipped, logged, the rest still counted
    with pytest.raises(ddc.LimitExceeded, match="unreadable lines"):
        ddc.check_budget("32", 1, 8)
    assert not fake.calls
    assert ddc.budget_view()["spent"] == 5.0                                         # accounting does not raise: enforcement keeps running
    assert ddc.cmd_watchdog(None) == 0


def test_the_ledger_append_is_one_fsynced_write_under_the_state_lock(monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(ddc.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    ddc.ledger_add({"event": "created", "dseq": "1"})
    assert synced


# ------------------------------------------------------------------ cross-process locking
CHILD = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, {root!r})
    from clouddeck import controller as ddc
    mode = sys.argv[1]
    if mode == "hold":
        with ddc.FileLock(wait_s=5):
            print("locked", flush=True)
            time.sleep(60)
    else:
        for _ in range(int(sys.argv[2])):
            ddc.update_state(lambda st: st.__setitem__("n", st.get("n", 0) + 1))
        print("done", flush=True)
""")


def child(*args):
    return subprocess.Popen([sys.executable, "-I", "-B", "-c", CHILD.format(root=str(ROOT)), *args], stdout=subprocess.PIPE, text=True,
                            env={**os.environ, "DDC_STATE_DIR": str(ddc.STATE_DIR), "DDC_SECRETS_DIR": str(ddc.SECRETS_DIR), "DDC_CONFIG": str(ddc.CONFIG_FILE)})


def test_the_command_lock_excludes_other_processes_and_a_crash_never_leaves_it_stuck():
    p = child("hold")
    try:
        assert p.stdout.readline().strip() == "locked"
        with pytest.raises(ddc.LockBusy, match="another ddc command holds"):
            with ddc.FileLock(wait_s=1):
                pass
    finally:
        p.kill()                                                                    # a crashed holder, no cleanup code runs
        p.wait()
    with ddc.FileLock(wait_s=5):                                                    # the OS released it: no stale-lock wait
        pass


def test_concurrent_read_modify_write_from_two_processes_loses_no_update():
    ddc.write_json(ddc.STATE_FILE, {"deps": {}, "n": 0})
    p = child("count", "60")
    try:
        for _ in range(60):
            ddc.update_state(lambda st: st.__setitem__("n", st.get("n", 0) + 1))
        assert p.stdout.readline().strip() == "done"
    finally:
        p.wait(timeout=60)
    assert ddc.load_state()["n"] == 120


def test_threads_in_one_process_also_serialize_and_the_lock_is_reentrant():
    ddc.write_json(ddc.STATE_FILE, {"deps": {}, "n": 0})

    def work():
        for _ in range(25):
            with ddc._state_lock:
                with ddc._state_lock:                                              # providers/akash.py nests update_dep inside the lock
                    ddc.update_state(lambda st: st.__setitem__("n", st.get("n", 0) + 1))
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert ddc.load_state()["n"] == 100


# ------------------------------------------------------------------ refresh_live: unknown is not closed
def test_empty_404_and_error_answers_keep_the_lease_marked_live(fake, capsys):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    for kind in ("empty", "notfound", "error"):
        fake.fail_next(f"GET /v1/deployments/{box['dseq']}", kind)
        ddc.refresh_live()
        rec = ddc.load_state()["deps"][box["dseq"]]
        assert rec["status"] == "ready", kind                                       # still marked live, still counted as billing
        assert box["dseq"] in ddc.active_deps() and ddc.budget_view()["committed"] > 0
    assert "keeping it marked live" in capsys.readouterr().out
    assert ddc.ledger_read()[0][-1]["event"] != "closed"
    fake.deployments[box["dseq"]]["state"] = "closed"                               # an EXPLICIT closed state does close the record
    ddc.refresh_live()
    assert ddc.load_state()["deps"][box["dseq"]]["status"] == "closed" and ddc.ledger_read()[0][-1]["by"] == "platform"


def test_a_lease_past_its_platform_limit_is_only_dropped_when_spend_rate_confirms(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.clock += 4.6 * 3600                                                        # beyond limit + 0.5 h
    fake.fail_next(f"GET /v1/deployments/{box['dseq']}", "empty", times=5)
    ddc.refresh_live()
    assert ddc.load_state()["deps"][box["dseq"]]["status"] == "ready"               # spend-rate still lists it: it is billing
    fake.deployments[box["dseq"]]["leased"] = False                                 # no longer billed
    fake.fail_next("GET /v1/spend-rate", "error")
    ddc.refresh_live()
    assert ddc.load_state()["deps"][box["dseq"]]["status"] == "ready"               # spend-rate unreadable: still unknown, not closed
    ddc.refresh_live()
    assert ddc.load_state()["deps"][box["dseq"]]["status"] == "closed" and ddc.ledger_read()[0][-1]["by"] == "expired"


def test_stop_does_not_trust_an_empty_answer_to_skip_the_delete(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.fail_next(f"GET /v1/deployments/{box['dseq']}", "empty", times=2)                    # refresh_live sees one, close_box the next
    ddc.cmd_stop(types.SimpleNamespace(workers_only=True, force=False))
    assert not fake.open_dseqs() and ("DELETE", f"/v1/deployments/{box['dseq']}") in fake.calls


# ------------------------------------------------------------------ legacy vs new state dir (RB-12)
def test_two_different_state_files_in_the_legacy_and_new_dirs_are_reported_not_guessed(tmp_path):
    home = tmp_path
    legacy, new = home / ".local" / "state" / "drill-dev-cloud", home / ".local" / "state" / "clouddeck"
    env = {"HOME": str(home)}
    assert ddc.state_dir(env, home, False) == new and ddc.state_dir_conflict(env, home, False) is None
    legacy.mkdir(parents=True)
    (legacy / "state.json").write_text('{"deps": {"1": {}}}')
    assert ddc.state_dir(env, home, False) == legacy and ddc.state_dir_conflict(env, home, False) is None      # legacy alone: used silently
    new.mkdir(parents=True)
    (new / "state.json").write_text('{"deps": {"1": {}}}')
    assert ddc.state_dir_conflict(env, home, False) is None                                                      # identical: nothing to decide
    (new / "state.json").write_text('{"deps": {"2": {}}}')
    msg = ddc.state_dir_conflict(env, home, False)
    assert "two different ddc state files" in msg and str(legacy) in msg and str(new) in msg
    assert ddc.state_dir_conflict({**env, "DDC_STATE_DIR": str(new)}, home, False) is None                       # an explicit dir always wins


def test_mutating_commands_refuse_on_a_state_dir_conflict_but_status_only_warns(monkeypatch, capsys):
    monkeypatch.setattr(ddc, "state_dir_conflict", lambda *a: "two different ddc state files exist")
    monkeypatch.setattr(ddc, "cmd_status", lambda a: 0)
    assert ddc.main(["status"]) == 0 and "warning: two different" in capsys.readouterr().out
    assert ddc.main(["stop", "--yes"]) == 1 and "two different" in capsys.readouterr().out


def test_custom_profiles_from_the_tui_are_validated_before_they_are_stored(fake):
    ddc.save_custom_profile("x8-16-50", {"cpu": 8, "mem": "16Gi", "disk": "50Gi", "max_usd_hr": 0.5})
    assert "x8-16-50" in ddc.read_json(ddc.STATE_DIR / "custom_profiles.json", {})
    for bad in ({"cpu": 8, "mem": "16Gi\n x: y", "disk": "50Gi", "max_usd_hr": 0.5}, {"cpu": 8, "mem": "16Gi", "disk": "50Gi", "max_usd_hr": 40}):
        with pytest.raises(ddc.DdcError):
            ddc.save_custom_profile("evil", bad)
    assert "evil" not in ddc.read_json(ddc.STATE_DIR / "custom_profiles.json", {}) and "evil" not in ddc.CFG["profiles"]
