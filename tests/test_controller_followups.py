"""Release follow-ups: the lease env is re-sent before every checkpoint, restored repos get their configured push remote, the PC-side
checkpoint clone never prompts or hangs, the command lock coexists with the pre-release tool's `ddc.lock`, `install --migrate` replaces a
stale clouddeck.cmd, a watchdog that cannot run raises an alert, scheduled watchdogs get --config, and config_warnings prints the real limits."""
import os
import subprocess
import time
import types

import pytest
from fake_akash import FakeAkash

from clouddeck.engine import ddc

_REAL_KEY_PROBLEM = ddc.key_problem


def _recording_ssh(monkeypatch, fail_env=False):
    calls = []

    def ssh(d, cmd, user="dev", input=None, timeout=600, check=True):
        calls.append((cmd, user, input))
        if fail_env and "/home/dev/.config/ddc/env" in cmd:
            raise ddc.DdcError("ssh root@h: exit 255: Connection reset")
        return "checkpoint_ok 2026-10-18T10:00:00Z abc reason=manual archives=1\n" if "checkpoint.sh" in cmd else ""
    monkeypatch.setattr(ddc, "ssh", ssh)
    return calls


# ------------------------------------------------------------------ R1: a changed state_repo reaches a running box before its checkpoint
def test_checkpoint_resends_the_lease_env_first(monkeypatch):
    monkeypatch.setitem(ddc.CFG, "state_repo", "newowner/state")
    calls = _recording_ssh(monkeypatch)
    assert ddc.run_checkpoint({"dseq": "42", "host": "h", "port": 1}, "manual").startswith("checkpoint_ok")
    (env_cmd, env_user, env_data), (cp_cmd, _, _) = calls
    assert "/home/dev/.config/ddc/env" in env_cmd and env_user == "dev"         # dev-owned: checkpoint.sh / agent.sh run as dev and source it
    assert b'DDC_STATE_REPO="newowner/state"' in env_data and b'DDC_DSEQ="42"' in env_data
    assert "checkpoint.sh manual" in cp_cmd


def test_checkpoint_still_runs_when_the_env_refresh_fails(monkeypatch):
    calls = _recording_ssh(monkeypatch, fail_env=True)
    assert ddc.run_checkpoint({"dseq": "42", "host": "h", "port": 1}, "manual").startswith("checkpoint_ok")
    assert "checkpoint.sh manual" in calls[-1][0]
    assert "could not refresh the lease env" in ddc.LOG.read_text(encoding="utf-8")


# ------------------------------------------------------------------ R2: restored repos push to the configured owner/repo
def test_push_remotes_are_set_for_push_repos_only(monkeypatch):
    calls = _recording_ssh(monkeypatch)
    ddc.fix_push_remotes({"host": "h"})
    (cmd, user, _), = calls
    assert user == "dev"
    assert "remote set-url origin git@github.com:example/web-app.git" in cmd   # the exact URL seed() sets
    assert "api-server" not in cmd                                             # push = false: left as restored (its push URL is disabled)


def test_provisioning_sets_push_remotes_after_restore_even_without_a_seed(monkeypatch):
    fake = FakeAkash(ddc, monkeypatch).install(repos=True)                     # every repo is already complete: restored, not seeded
    ddc.cmd_start(types.SimpleNamespace(profile="32", workers=0, worker_profile=None))
    cmds = [c for _, c in fake.ssh_log]
    restore = next(i for i, c in enumerate(cmds) if "restore.sh" in c)
    fix = next(i for i, c in enumerate(cmds) if "remote set-url origin git@github.com:example/web-app.git" in c)
    assert restore < fix


# ------------------------------------------------------------------ R3: the PC-side clone is bounded and never prompts
def test_verify_checkpoint_clone_timeout_fails_closed(monkeypatch):
    seen = {}

    def run(argv, **k):
        seen.update(k)
        raise subprocess.TimeoutExpired(argv, k.get("timeout"))
    monkeypatch.setattr(ddc.subprocess, "run", run)
    with pytest.raises(ddc.DdcError, match="timed out after 600 s.*NOT verified"):
        ddc.verify_checkpoint("abc")
    assert seen["timeout"] == 600 and seen["env"]["GIT_TERMINAL_PROMPT"] == "0" and seen["env"]["GCM_INTERACTIVE"] == "never"


# ------------------------------------------------------------------ M1: the pre-release tool's ddc.lock file
def test_command_lock_uses_its_own_file_and_waits_for_a_fresh_legacy_lock():
    legacy = ddc.STATE_DIR / "ddc.lock"
    ddc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with ddc.FileLock(wait_s=1) as lk:
        assert lk.p.name == "ddc.flock"
    assert not legacy.exists()                                                     # nothing left that the legacy tool would read as held
    legacy.write_text("1234 0")                                                    # a legacy command is running
    t0 = time.monotonic()
    with pytest.raises(ddc.LockBusy, match="pre-release"):
        with ddc.FileLock(wait_s=0.5):
            pass
    assert time.monotonic() - t0 >= 0.4                                            # waited, as for any other holder
    fd = ddc._lock_fd(ddc.STATE_DIR / "ddc.flock", 0)                             # our own lock was released on the refusal
    assert fd is not None
    ddc._unlock_fd(fd)
    old = time.time() - ddc.LEGACY_LOCK_STALE_S - 60
    os.utime(legacy, (old, old))                                                   # a crashed legacy run: stale, ignored
    with ddc.FileLock(wait_s=0):
        pass
    assert legacy.exists()                                                         # ...but not deleted by us
    with ddc.FileLock("jobs/t1.lock", wait_s=0) as lk:                             # other locks keep their name and never look at the legacy file
        assert lk.p.name == "t1.lock"


def test_an_empty_ddc_lock_left_by_an_os_lock_build_does_not_block():
    ddc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    legacy = ddc.STATE_DIR / "ddc.lock"
    legacy.write_text("")                                                          # recent, but not the pre-release tool's `<pid> <time>`
    old = time.time() - 60
    os.utime(legacy, (old, old))
    with ddc.FileLock(wait_s=0):
        pass


# ------------------------------------------------------------------ M2 / M3: Windows installer
def _installer(monkeypatch, tmp_path):
    monkeypatch.setattr(ddc, "HOME", tmp_path)
    (tmp_path / "Desktop").mkdir()
    calls = []

    def run(argv, **k):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=1 if argv[:2] == ["schtasks", "/Query"] else 0, stdout=b"", stderr=b"")
    monkeypatch.setattr(ddc, "launcher", lambda: ["C:/Py/python.exe", "-m", "clouddeck.controller"])  # the task line must not depend on where the test venv lives
    monkeypatch.setattr(ddc.subprocess, "run", run)
    return calls


def test_migrate_rewrites_a_stale_clouddeck_shim_and_the_task_gets_the_config(tmp_path, monkeypatch):
    calls = _installer(monkeypatch, tmp_path)
    (tmp_path / "bin").mkdir()
    stale = tmp_path / "bin" / "clouddeck.cmd"
    stale.write_text('@echo off\r\nset "PYTHONPATH=C:\\old\\worktree;%PYTHONPATH%"\r\npython -m clouddeck %*\r\n')
    ddc.install_windows()
    assert "old\\worktree" in stale.read_text()                                     # without --migrate: untouched
    create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
    assert f'"--config" "{ddc.CONFIG_FILE}" watchdog' in create[-1]
    assert [str(p) for p, _ in ddc.windows_shims(True)] == [str(tmp_path / "bin" / n) for n in ("claude-cloud.cmd", "ddc.cmd", "clouddeck.cmd")]
    ddc.install_windows(migrate=True)
    assert "old\\worktree" not in stale.read_text() and "-m clouddeck %*" in stale.read_text()
    assert ddc.windows_shims(True)[-1][0].name != "clouddeck.cmd"                  # now current: nothing more to rewrite


def test_install_dry_run_lists_the_shims_it_would_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "WIN", True)
    monkeypatch.setattr(ddc, "HOME", tmp_path)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "clouddeck.cmd").write_text("@echo off\r\npython -m clouddeck %*\r\n")
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=x\n")
    monkeypatch.setattr(ddc, "repo_deploy_keys", lambda: [])
    ddc.cmd_install(types.SimpleNamespace(dry_run=True, migrate=True, yes=False))
    out = capsys.readouterr().out
    assert str(tmp_path / "bin" / "clouddeck.cmd") in out and str(tmp_path / "bin" / "ddc.cmd") in out


def test_posix_watchdog_command_carries_the_config():
    cmd = ddc._watchdog_command()
    assert cmd[-2:] == ["watchdog", "--once"] and cmd[cmd.index("--config") + 1] == str(ddc.CONFIG_FILE)


# ------------------------------------------------------------------ M3: a watchdog that cannot run says so
def test_watchdog_alerts_when_the_config_is_broken(monkeypatch):
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "cannot read config x.toml: bad")
    notified = []
    monkeypatch.setattr(ddc, "_notify", notified.append)
    assert ddc.main(["watchdog", "--once"]) == 1
    assert notified and "watchdog NOT running" in notified[0] and "cannot read config" in notified[0]
    assert ddc.main(["watchdog", "--once"]) == 1 and len(notified) == 1           # throttled like other watchdog alerts
    assert ddc.main(["status"]) == 1 and len(notified) == 1                        # an interactive command just prints the error


def test_watchdog_alerts_on_a_state_dir_conflict(monkeypatch):
    monkeypatch.setattr(ddc, "state_dir_conflict", lambda *a: "two different ddc state files exist")
    notified = []
    monkeypatch.setattr(ddc, "_notify", notified.append)
    assert ddc.main(["watchdog"]) == 1
    assert notified == ["watchdog NOT running: two different ddc state files exist"]


# ------------------------------------------------------------------ M5: the warning shows the limits that apply
def test_budget_guard_off_warning_prints_the_effective_limits(monkeypatch):
    monkeypatch.setitem(ddc.CFG, "budget_guard", False)
    monkeypatch.setitem(ddc.CFG, "max_total_leases", 3)
    monkeypatch.setitem(ddc.CFG, "max_workers", 2)
    w = next(x for x in ddc.config_warnings() if x.startswith("budget_guard"))
    assert "3 leases, 2 workers" in w and "8 leases" not in w


def test_a_watchdog_command_over_the_schtasks_limit_fails_with_a_reason(tmp_path, monkeypatch):
    calls = _installer(monkeypatch, tmp_path)
    monkeypatch.setattr(ddc, "launcher", lambda: ["C:/" + "p" * 250 + "/python.exe", "-m", "clouddeck.controller"])
    with pytest.raises(ddc.DdcError, match="over the 261 schtasks allows"):
        ddc.install_windows()
    assert not [c for c in calls if c[:2] == ["schtasks", "/Create"]]


# ------------------------------------------------------------------ a key ssh would refuse is caught before any lease exists
@pytest.mark.skipif(not ddc.shutil.which(ddc.SSH_KEYGEN), reason="needs ssh-keygen")
def test_preflight_loads_the_access_key_like_ssh_does(monkeypatch, tmp_path):
    monkeypatch.setattr(ddc, "key_problem", _REAL_KEY_PROBLEM)                     # conftest stubs it out for the placeholder fixture keys
    key = tmp_path / "access"
    subprocess.run([ddc.SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    if os.name == "nt":
        ddc.owner_only(key)
    else:
        key.chmod(0o600)
    assert ddc.key_problem(key) is None
    if os.name == "nt":                                                             # another account can read it: ssh refuses such a key
        subprocess.run(["icacls", str(key), "/grant", "*S-1-5-32-545:R"], check=True, capture_output=True)   # BUILTIN Users
    else:
        key.chmod(0o644)
    assert "ssh would refuse the key" in ddc.key_problem(key)
    key.write_text("not a key")
    assert "ssh would refuse the key" in ddc.key_problem(key)


@pytest.mark.skipif(not ddc.shutil.which(ddc.SSH_KEYGEN), reason="needs ssh-keygen")
def test_a_passphrase_protected_key_is_not_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(ddc, "key_problem", _REAL_KEY_PROBLEM)
    key = tmp_path / "access"
    subprocess.run([ddc.SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "pw-for-test", "-f", str(key)], check=True)
    if os.name == "nt":
        ddc.owner_only(key)
    else:
        key.chmod(0o600)
    assert ddc.key_problem(key) is None                                             # ssh-agent may hold it: permissions are what matter


def test_a_refused_key_stops_start_before_any_lease_exists(monkeypatch):
    fake = FakeAkash(ddc, monkeypatch).install(real_preflight=True)
    monkeypatch.setattr(ddc, "preflight", lambda role="dev": [f"ssh would refuse the key {ddc.KEYS / 'access'} (bad permissions)"])
    with pytest.raises(ddc.DdcError, match="preflight failed"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=0, worker_profile=None))
    assert not fake.deployments and not ddc.active_deps()


def test_preflight_reports_what_key_problem_finds(monkeypatch):
    monkeypatch.setattr(ddc, "key_problem", lambda key: f"ssh would refuse the key {key} (bad permissions)")
    (ddc.KEYS / "access").write_text("x")
    assert any("ssh would refuse the key" in p for p in ddc.preflight("dev"))


def test_an_empty_ddc_lock_younger_than_5_s_is_held():
    ddc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ddc.STATE_DIR / "ddc.lock").write_text("")                                    # the pre-release tool between its O_EXCL create and its write
    with pytest.raises(ddc.LockBusy):
        with ddc.FileLock(wait_s=0):
            pass


def test_scale_and_start_default_to_an_existing_profile(monkeypatch):
    monkeypatch.setitem(ddc.CFG, "profiles", {"t": {"cpu": 2, "mem": "4Gi", "disk": "10Gi", "max_usd_hr": 0.15}})
    assert ddc.default_profile() == "t" and ddc.profile_label("t") == "t (2 vCPU)"
    monkeypatch.setitem(ddc.CFG, "profiles", {"16": {"cpu": 16}, "32": {"cpu": 32.0}})
    assert ddc.default_profile() == "32" and ddc.profile_label("32") == "32 vCPU"
