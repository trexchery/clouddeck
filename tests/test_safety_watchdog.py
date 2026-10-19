"""The watchdog (idle / runtime / budget / stale-lease enforcement), its failure direction, alerts, and the Linux/WSL scheduler install."""
import shlex
import subprocess
import sys
import types

import pytest

from clouddeck.engine import ddc

_REAL_NOTIFY = ddc._notify
ARGS = types.SimpleNamespace(profile="32", workers=0, worker_profile=None)


@pytest.fixture
def fleet(fake):
    ddc.cmd_start(ARGS)
    ddc.cmd_scale(types.SimpleNamespace(n=2, profile="32"))
    assert len(fake.open_dseqs()) == 3
    fake.agent.update(idle_min=3, active_reason="cpu=2.0cores")
    return fake


def run_wd():
    return ddc.cmd_watchdog(None)


def alerts():
    return " | ".join(ddc.recent_alerts(50))


# ------------------------------------------------------------------ enforcement
def test_nothing_running_is_a_quiet_no_op(fake):
    assert run_wd() == 0 and not fake.calls


def test_an_active_box_is_left_alone(fleet):
    assert run_wd() == 0 and len(fleet.open_dseqs()) == 3 and ddc.load_state().get("dev_unreachable_since") is None


def test_idle_shutdown_requested_by_the_agent_stops_everything_after_a_verified_checkpoint(fleet):
    fleet.agent.update(shutdown_requested="idle", idle_min=45, active_reason="")
    assert run_wd() == 0
    assert not fleet.open_dseqs()
    assert any("checkpoint.sh final-stop" in c for h, c in fleet.ssh_log)
    assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["last_result"] == "stop (idle)"


def test_idle_stop_with_a_failing_checkpoint_keeps_the_box_alerts_and_counts_as_a_failure(fleet):
    fleet.agent.update(shutdown_requested="idle")
    fleet.checkpoint = "fail"
    assert run_wd() == 1                                              # cmd_stop raised: reported, not swallowed
    assert fleet.open_dseqs() and "final checkpoint did not verify" in alerts() and "watchdog is failing" in alerts()
    assert ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["fails"] == 1


def test_snooze_from_clouddeck_holds_an_idle_request_but_not_the_runtime_limit(fleet):
    fleet.agent.update(shutdown_requested="idle")
    ddc.update_state(lambda st: st.__setitem__("idle_hold_until", fleet.clock + 1800))
    assert run_wd() == 0 and len(fleet.open_dseqs()) == 3
    fleet.clock += 8 * 3600 - 600                                      # inside the last 15 minutes of max_runtime_hours
    fleet.agent["shutdown_requested"] = ""
    assert run_wd() == 0 and not fleet.open_dseqs()


def test_runtime_limit_stops_the_box_even_without_an_agent_request(fleet):
    fleet.clock += 7.9 * 3600
    assert run_wd() == 0 and not fleet.open_dseqs()


def test_budget_exhaustion_force_stops_and_alerts(fleet):
    ddc.CFG["budget_usd"] = 1.0
    fleet.clock += 3600                                                # ~$0.90 spent + a quarter hour of burn >= $1
    fleet.checkpoint = "fail"                                          # even a failing checkpoint must not keep money burning
    assert run_wd() == 0
    assert not fleet.open_dseqs() and "budget nearly exhausted" in alerts()


def test_eighty_percent_budget_alert_fires_once(fleet):
    ddc.CFG["budget_usd"] = 10.0
    fleet.clock += 3600
    ddc.update_dep(ddc.dev_box()["dseq"], usd_hr=8.5)                  # ~$9 of $10 spent
    ddc.CFG["budget_guard"] = False                                    # (with the guard on the budget stop would fire first)
    run_wd()
    run_wd()
    assert alerts().count("80% of the $10 budget") == 1


def test_leases_that_never_became_ready_are_closed_after_30_minutes(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    ddc.update_dep(box["dseq"], status="leased")
    ddc.update_dep(box["dseq"], created_at=fake.clock)
    fake.clock += 20 * 60
    run_wd()
    assert fake.open_dseqs() == [box["dseq"]]                          # too young
    fake.clock += 11 * 60
    run_wd()
    assert not fake.open_dseqs() and ddc.load_state()["deps"][box["dseq"]]["status"] == "closed"


def test_workers_without_a_dev_box_are_closed(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    run_wd()
    assert not fake.open_dseqs() and ddc.load_state()["deps"][box["dseq"]]["status"] == "closed"


def test_idle_workers_are_closed_but_not_while_the_box_is_busy(fleet):
    fleet.clock += 3600
    fleet.agent.update(dist_idle_min=30, active_reason="procs")
    run_wd()
    assert len(fleet.open_dseqs()) == 3
    fleet.agent.update(active_reason="")
    run_wd()
    assert fleet.open_dseqs() == [ddc.dev_box()["dseq"]] and not ddc.workers()


def test_unreachable_dev_box_is_tolerated_for_30_minutes_then_force_closed(fleet):
    host = ddc.dev_box()["host"]
    fleet.unreachable.add(host)
    assert run_wd() == 0 and len(fleet.open_dseqs()) == 3
    fleet.clock += 31 * 60
    assert run_wd() == 0
    assert "unreachable for 30 min" in alerts() and not fleet.open_dseqs()


# ------------------------------------------------------------------ SAF-25 / SAF-26: stale agent, persistent checkpoint failure
def box_clock(fleet, offset=0.0, frozen=False):
    """The dev box's agent writes `now` from the BOX's clock: `offset` seconds off the PC's, or `frozen` = it stopped updating."""
    import json
    real, first = fleet.ssh, []

    def served(d, cmd, **k):
        out = real(d, cmd, **k)
        if "cat /run/ddc/state.json" in cmd:
            j = json.loads(out)
            first.append(j["now"])
            j["now"] = first[0] if frozen else j["now"] + offset
            return json.dumps(j)
        return out
    fleet.mp.setattr(ddc, "ssh", served)


def test_a_dead_activity_agent_is_alerted_restarted_and_its_stale_fields_are_ignored(fleet):
    box_clock(fleet, frozen=True)
    assert run_wd() == 0 and fleet.agent_restarts == 0                 # first sight of the agent: nothing to compare with yet
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    fleet.clock += 20 * 60                                             # its `now` has not moved for 20 minutes
    assert run_wd() == 0
    assert len(fleet.open_dseqs()) == 3                                # an idle request from a dead agent is not acted on
    assert "activity agent has not updated for 20 min" in alerts() and fleet.agent_restarts == 1
    run_wd()
    assert alerts().count("activity agent has not updated") == 1       # throttled: one alert per hour, not per 5-minute run


def test_a_box_clock_that_is_off_does_not_make_a_working_agent_look_dead(fleet):
    box_clock(fleet, offset=-7 * 60)                                   # the box's clock is 7 minutes behind this PC's, but it keeps ticking
    run_wd()
    fleet.clock += 300
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    assert run_wd() == 0 and not fleet.open_dseqs()                    # the idle request is trusted and carried out
    assert "has not updated" not in alerts() and fleet.agent_restarts == 0


def test_persistent_checkpoint_failure_is_alerted_only_after_two_intervals(fleet):
    fleet.agent["checkpoint_failed"] = True
    run_wd()
    assert "checkpoints have been failing" not in alerts()
    fleet.clock += 31 * 60                                             # > 2 x checkpoint_minutes (15)
    run_wd()
    assert "checkpoints have been failing for 31 min" in alerts()
    fleet.agent["checkpoint_failed"] = False
    run_wd()
    assert "ckpt_failed_since" not in ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})


# ------------------------------------------------------------------ checkpoints disabled
def test_idle_with_checkpoints_disabled_alerts_instead_of_destroying_the_workspace(fleet):
    ddc.CFG["state_repo"] = ""
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    assert run_wd() == 0 and len(fleet.open_dseqs()) == 3
    assert "checkpoints are disabled" in alerts() and "--discard-workspace" in alerts()
    ddc.CFG["idle_discards_workspace"] = True                          # the owner's explicit choice
    assert run_wd() == 0 and not fleet.open_dseqs()


def test_the_runtime_limit_closes_a_checkpoint_less_box_because_the_platform_would_anyway(fleet):
    ddc.CFG["state_repo"] = ""
    fleet.clock += 7.9 * 3600
    assert run_wd() == 0 and not fleet.open_dseqs()


# ------------------------------------------------------------------ failure direction
def test_a_failing_watchdog_alerts_and_after_three_failures_attempts_a_safe_stop(fleet, monkeypatch):
    def boom():
        raise RuntimeError("api exploded")
    with monkeypatch.context() as m:
        m.setattr(ddc, "watchdog_pass", boom)
        assert run_wd() == 1 and run_wd() == 1
        assert len(fleet.open_dseqs()) == 3 and alerts().count("the watchdog is failing") == 1      # throttled alert, no stop yet
        assert run_wd() == 1                                           # third consecutive failure: safe stop, checkpoint verifies
    assert not fleet.open_dseqs()
    assert any("final-stop" in c for h, c in fleet.ssh_log)
    assert run_wd() == 0 and ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["fails"] == 0       # a good run resets the counter


def test_the_safe_stop_never_happens_without_a_verified_checkpoint_and_says_the_box_is_still_running(fleet, monkeypatch):
    monkeypatch.setattr(ddc, "watchdog_pass", lambda: (_ for _ in ()).throw(RuntimeError("api exploded")))
    fleet.checkpoint = "fail"
    for _ in range(3):
        run_wd()
    assert ddc.dev_box()["dseq"] in fleet.open_dseqs()
    assert "did NOT complete" in alerts() and "STILL RUNNING" in alerts()


def test_corrupt_state_makes_the_watchdog_fail_loudly_not_silently(fake):
    ddc.STATE_FILE.write_text("{ broken")
    assert run_wd() == 1 and "will NOT create leases" in alerts()


def test_a_held_lock_is_logged_and_alerted_after_30_minutes_of_skips(fleet, monkeypatch, capsys):
    real = ddc.FileLock
    monkeypatch.setattr(ddc, "FileLock", lambda name="ddc.lock", wait_s=900: real(name, min(wait_s, 0.2)))
    with real(wait_s=1):
        for _ in range(5):
            assert run_wd() == 0
        assert "holds the lock (skipped 5 run(s)" in capsys.readouterr().out and "could not run" not in alerts()
        run_wd()
    assert "watchdog could not run for 30 min" in alerts()
    assert run_wd() == 0 and ddc.read_json(ddc.STATE_DIR / "watchdog.json", {})["skips"] == 0


def test_api_down_does_not_switch_off_the_ssh_based_checks(fleet, monkeypatch):
    fleet.agent.update(shutdown_requested="idle", active_reason="")
    monkeypatch.setattr(ddc, "refresh_live", lambda: (_ for _ in ()).throw(ddc.DdcError("api down")))
    assert run_wd() == 0 and not fleet.open_dseqs()                    # the idle stop still happened, with a warning and an alert
    assert "cannot reach the Akash API" in alerts()


def test_watchdog_once_flag_is_accepted_and_runs_one_pass(fake, capsys):
    assert ddc.main(["watchdog", "--once"]) == 0


# ------------------------------------------------------------------ alerts
def test_alert_is_logged_recorded_and_throttled_per_key(fake):
    ddc.alert_once("k", "first", 3600)
    ddc.alert_once("k", "second", 3600)
    fake.clock += 3601
    ddc.alert_once("k", "third", 3600)
    assert alerts().count("first") == 1 and "second" not in alerts() and "third" in alerts()


def test_alert_command_gets_the_message_as_its_last_argument(monkeypatch):
    ran = []
    monkeypatch.setattr(ddc, "_notify", _REAL_NOTIFY)
    monkeypatch.setattr(ddc.subprocess, "run", lambda argv, **k: ran.append(list(argv)) or types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(ddc, "WIN", False)
    monkeypatch.setattr(ddc.shutil, "which", lambda x: None)
    ddc.CFG["alert_command"] = "/usr/local/bin/hook --flag"
    ddc.alert("balance low")
    assert ran == [["/usr/local/bin/hook", "--flag", "balance low"]]
    monkeypatch.setattr(ddc.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no such command")))
    ddc.alert("still must not raise")                                  # a broken notifier cannot break enforcement


def test_dispatching_jobs_marks_worker_activity_so_the_box_is_not_closed_as_idle(fake, monkeypatch):
    d = {"host": "dev.example", "port": 22, "dseq": "1"}
    ddc.touch_worker_activity(d)
    assert fake.ssh_log[-1][1].startswith("touch /run/ddc/worker_activity")
    monkeypatch.setattr(ddc, "ssh", lambda *a, **k: (_ for _ in ()).throw(ddc.DdcError("down")))
    ddc.touch_worker_activity(d)                                       # best effort, never raises


# ------------------------------------------------------------------ Linux / WSL scheduler
class Sched:
    """Fake systemctl / crontab behind subprocess.run, with HOME redirected so nothing real is touched."""

    def __init__(self, monkeypatch, tmp_path, systemd=True, cron=True, platform="linux"):
        self.cron_text, self.calls, self.systemd, self.cron = "", [], systemd, cron
        monkeypatch.setattr(ddc, "WIN", False)
        monkeypatch.setattr(ddc, "HOME", tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(ddc, "PLATFORM", platform)
        monkeypatch.setattr(ddc.shutil, "which", lambda x: f"/usr/bin/{x}" if (x == "systemctl" and systemd) or (x == "crontab" and cron) else None)
        monkeypatch.setattr(ddc.subprocess, "run", self.run)
        monkeypatch.setattr(ddc, "launcher", lambda: [sys.executable, "-m", "clouddeck.controller"])
        monkeypatch.setenv("DDC_STATE_DIR", str(ddc.STATE_DIR))

    def run(self, argv, **k):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "systemctl":
            rc = 0 if self.systemd else 1
            return subprocess.CompletedProcess(argv, rc, b"", b"")
        if argv[0] == "crontab":
            if argv[1] == "-l":
                return subprocess.CompletedProcess(argv, 0 if self.cron_text else 1, self.cron_text.encode(), b"" if self.cron_text else b"no crontab for dev")
            self.cron_text = k["input"].decode()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        raise AssertionError(argv)


def test_install_registers_a_systemd_user_timer_and_uninstall_removes_it(monkeypatch, tmp_path):
    s = Sched(monkeypatch, tmp_path)
    assert ddc.install_watchdog() == "systemd"
    d = tmp_path / "cfg" / "systemd" / "user"
    service, timer = (d / "clouddeck-watchdog.service").read_text(), (d / "clouddeck-watchdog.timer").read_text()
    assert "ExecStart=" in service and "clouddeck.controller" in service and '"watchdog" "--once"' in service and "Type=oneshot" in service
    assert "OnUnitActiveSec=5min" in timer and "WantedBy=timers.target" in timer
    assert ["systemctl", "--user", "enable", "--now", "clouddeck-watchdog.timer"] in s.calls
    ddc.install_watchdog()                                             # idempotent: same files, no duplicate registration
    assert (d / "clouddeck-watchdog.timer").read_text() == timer and not s.cron_text
    assert ddc.uninstall_watchdog() == ["systemd user timer clouddeck-watchdog"]
    assert not list(d.glob("clouddeck-watchdog.*")) and ["systemctl", "--user", "disable", "--now", "clouddeck-watchdog.timer"] in s.calls


def test_install_falls_back_to_one_marked_crontab_block_and_leaves_the_rest_alone(monkeypatch, tmp_path):
    s = Sched(monkeypatch, tmp_path, systemd=False)
    s.cron_text = "MAILTO=me\n0 3 * * * backup.sh\n"
    assert ddc.install_watchdog() == "cron"
    assert ddc.install_watchdog() == "cron"                            # again: replaced, not duplicated
    t = s.cron_text
    assert t.count(ddc.CRON_BEGIN) == 1 and t.count(ddc.CRON_END) == 1 and t.startswith("MAILTO=me\n0 3 * * * backup.sh\n")
    line = [x for x in t.splitlines() if x.startswith("*/5 ")][0]
    assert f"clouddeck.controller --config {shlex.quote(str(ddc.CONFIG_FILE))} watchdog --once" in line and "env " in line and "PATH=" in line and "watchdog.log" in line
    assert ddc.watchdog_installed() is True
    assert ddc.uninstall_watchdog() == ["crontab block"]
    assert s.cron_text == "MAILTO=me\n0 3 * * * backup.sh\n" and ddc.watchdog_installed() is False


def test_a_watchdog_registered_for_another_config_does_not_count(monkeypatch, tmp_path):
    s = Sched(monkeypatch, tmp_path, systemd=False)
    assert ddc.install_watchdog() == "cron" and ddc.watchdog_installed() is True
    s.cron_text = s.cron_text.replace(shlex.quote(str(ddc.CONFIG_FILE)), "/other/install/config.toml")
    assert ddc.watchdog_installed() is False                           # it enforces some other install's leases, not ours


def test_windows_task_counts_only_when_it_runs_this_config(monkeypatch, tmp_path):
    xml = {}

    def run(argv, **k):
        t = argv[3]
        return types.SimpleNamespace(returncode=0 if t in xml else 1, stdout=xml.get(t, ""), stderr="")
    monkeypatch.setattr(ddc, "WIN", True)
    monkeypatch.setattr(ddc.subprocess, "run", run)
    cfg = ddc.CONFIG_FILE
    xml[ddc.LEGACY_TASK] = '<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>"C:\\elsewhere\\ddc.py" watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is False                           # the legacy task of another checkout
    xml[ddc.LEGACY_TASK] = f'<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>"{cfg.parent / "ddc.py"}" watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is True                            # the legacy task next to this config
    del xml[ddc.LEGACY_TASK]
    xml[ddc.TASK] = '<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>-m clouddeck.controller "--config" "C:\\x\\config.toml" watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is False
    xml[ddc.TASK] = f'<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>-m clouddeck.controller &quot;--config&quot; &quot;{cfg}&quot; watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is True
    xml[ddc.TASK] = f'<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>-m clouddeck.controller "--config" "{cfg.parent}{ddc.os.sep}.{ddc.os.sep}{cfg.name}" watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is True                            # real schtasks XML (quotes not escaped), same file spelled differently
    xml[ddc.TASK] = f'<Exec><Command>"C:\\Py\\pythonw.exe"</Command><Arguments>-m clouddeck.controller "--config" "{cfg}.old" watchdog</Arguments></Exec>'
    assert ddc.watchdog_installed() is False                           # a longer path that merely starts with ours


def test_only_our_crontab_block_counts(monkeypatch, tmp_path):
    s = Sched(monkeypatch, tmp_path, systemd=False)
    ddc.install_watchdog()
    s.cron_text = f"0 1 * * * other-tool --config /etc/other.toml\n{s.cron_text}0 2 * * * echo {ddc.CONFIG_FILE}\n"
    assert ddc.watchdog_installed() is True                            # an unrelated job's --config does not hide our block
    begin, end = s.cron_text.index(ddc.CRON_BEGIN), s.cron_text.index(ddc.CRON_END)
    s.cron_text = s.cron_text[:begin] + s.cron_text[begin:end].replace(shlex.quote(str(ddc.CONFIG_FILE)), "/x/config.toml") + s.cron_text[end:]
    assert ddc.watchdog_installed() is False                           # ...and a mention of our path outside it does not count


def test_systemd_unit_counts_only_when_it_runs_this_config(monkeypatch, tmp_path):
    Sched(monkeypatch, tmp_path)
    assert ddc.install_watchdog() == "systemd" and ddc.watchdog_installed() is True
    unit = tmp_path / "cfg" / "systemd" / "user" / "clouddeck-watchdog.service"
    unit.write_text(unit.read_text().replace(ddc._sd_quote(str(ddc.CONFIG_FILE)), '"/other/config.toml"'))
    assert ddc.watchdog_installed() is False


def test_install_without_any_scheduler_or_on_macos_warns_loudly(monkeypatch, tmp_path, capsys):
    Sched(monkeypatch, tmp_path, systemd=False, cron=False)
    assert ddc.install_watchdog() == "none"
    assert "WARNING: no watchdog installed" in capsys.readouterr().out
    monkeypatch.setattr(ddc, "PLATFORM", "darwin")
    assert ddc.install_watchdog() == "unsupported" and ddc.watchdog_installed() is None
    assert "launchd is not supported" in capsys.readouterr().out


def test_start_warns_when_no_watchdog_is_registered(fake, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: False)
    ddc.cmd_start(ARGS)
    assert "no watchdog is registered" in capsys.readouterr().out
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: None)       # unknown (macOS): no false alarm
    ddc.cmd_start(ARGS)
    assert "no watchdog is registered" not in capsys.readouterr().out


def test_uninstall_on_windows_removes_only_the_new_task_and_reports_the_legacy_one(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ddc, "WIN", True)
    monkeypatch.setattr(ddc.subprocess, "run", lambda argv, **k: calls.append(list(argv)) or types.SimpleNamespace(returncode=0))
    assert ddc.uninstall_watchdog() == ["scheduled task CloudDeck-Watchdog"]
    assert ["schtasks", "/Delete", "/F", "/TN", "CloudDeck-Watchdog"] in calls and not [c for c in calls if "DrillDevCloud-Watchdog" in c and "/Delete" in c]
    assert "legacy task DrillDevCloud-Watchdog is still registered" in capsys.readouterr().out


def test_install_command_registers_the_watchdog_on_linux_and_has_no_github_side_effect_in_dry_run(monkeypatch, tmp_path, capsys):
    ddc.CFG["state_repo"] = "o/state"
    ddc.CFG["repos"] = [{"name": "web", "path": "/x", "github": "o/web", "push": True}]
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=k\nDDC_BACKUP_PASSPHRASE=p\n")
    ran = []
    monkeypatch.setattr(ddc.subprocess, "run", lambda argv, **k: ran.append(list(argv)) or types.SimpleNamespace(returncode=0, stdout=b""))
    assert ddc.main(["install", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "create the private repo o/state" in out and "WRITE deploy key to o/state" in out and "WRITE deploy key to o/web" in out
    assert not ran                                                     # nothing was run: no gh, no ssh-keygen, no schtasks
    ddc.CFG["state_repo"] = "OWNER/your-private-state-repo"
    assert ddc.main(["install", "--yes"]) == 1 and "documentation placeholder" in capsys.readouterr().out and not ran
    ddc.CFG["state_repo"] = "o/state"
    monkeypatch.setattr(ddc.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    assert ddc.main(["install"]) == 1 and "pass --yes" in capsys.readouterr().out and not ran               # no silent GitHub writes
