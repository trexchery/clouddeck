"""Config / directory resolution, `ddc init`, empty state_repo, deploy keys, heal hook, installer and packaging layout.
Everything runs against temp dirs and mocked subprocess/API calls: no real state, secrets, ssh, gpg or network."""
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

from clouddeck import cli
from clouddeck.engine import ddc

POSIX = os.name != "nt"


# ------------------------------------------------------------------ isolation guard
def test_suite_never_points_at_real_state_or_secrets():
    tmp = Path(tempfile.gettempdir()).resolve()
    for p in (ddc.STATE_DIR, ddc.SECRETS_DIR, ddc.CONFIG_FILE):
        assert tmp in Path(p).resolve().parents


# ------------------------------------------------------------------ config lookup
def test_config_lookup_order(tmp_path):
    home, checkout = tmp_path / "home", tmp_path / "co"
    user = home / ".config" / "clouddeck" / "config.toml"
    legacy_home = home / ".config" / "drill-dev-cloud" / "config.toml"
    legacy_checkout = checkout / "config.toml"
    for p in (user, legacy_home, legacy_checkout):
        p.parent.mkdir(parents=True)
        p.write_text("")
    find = lambda env, co=checkout: ddc.find_config(env, home, False, co)  # noqa: E731
    explicit = tmp_path / "x.toml"
    assert find({"DDC_CONFIG": str(explicit)}) == explicit.resolve()      # even if it does not exist (reported later)
    assert find({}) == user
    user.unlink()
    assert find({}) == legacy_checkout                                    # the original checkout layout still works
    assert find({}, None) == legacy_home                                  # installed package: no checkout location
    legacy_home.unlink()
    assert find({}, None) is None
    assert find({"XDG_CONFIG_HOME": str(tmp_path / "xdg")}, None) is None


def test_user_config_dir_per_os(tmp_path):
    assert ddc.user_config_dir({"APPDATA": str(tmp_path / "ad")}, tmp_path, True) == tmp_path / "ad" / "clouddeck"
    assert ddc.user_config_dir({"XDG_CONFIG_HOME": str(tmp_path / "x")}, tmp_path, False) == tmp_path / "x" / "clouddeck"
    assert ddc.user_config_dir({}, tmp_path, False) == tmp_path / ".config" / "clouddeck"


def test_secrets_dir_prefers_existing_legacy(tmp_path):
    assert ddc.secrets_dir({"DDC_SECRETS_DIR": str(tmp_path / "s")}, tmp_path) == tmp_path / "s"
    assert ddc.secrets_dir({}, tmp_path) == tmp_path / ".config" / "clouddeck"
    (tmp_path / ".config" / "drill-dev-cloud").mkdir(parents=True)
    assert ddc.secrets_dir({}, tmp_path) == tmp_path / ".config" / "drill-dev-cloud"       # existing installs keep their keys
    assert ddc.secrets_dir({"DDC_SECRETS_DIR": str(tmp_path / "s")}, tmp_path) == tmp_path / "s"


def test_state_dir_defaults_and_legacy(tmp_path):
    home = tmp_path
    win_env = {"LOCALAPPDATA": str(tmp_path / "local")}
    assert ddc.state_dir({"DDC_STATE_DIR": str(tmp_path / "s")}, home, True) == tmp_path / "s"
    assert ddc.state_dir(win_env, home, True) == tmp_path / "local" / "clouddeck"
    assert ddc.state_dir({}, home, False) == home / ".local" / "state" / "clouddeck"
    assert ddc.state_dir({"XDG_STATE_HOME": str(tmp_path / "xs")}, home, False) == tmp_path / "xs" / "clouddeck"
    (tmp_path / "local" / "drill-dev-cloud").mkdir(parents=True)
    assert ddc.state_dir(win_env, home, True) == tmp_path / "local" / "drill-dev-cloud"    # live ledger/state.json keep being used
    (home / ".local" / "state" / "drill-dev-cloud").mkdir(parents=True)
    assert ddc.state_dir({}, home, False) == home / ".local" / "state" / "drill-dev-cloud"


def test_load_config_defaults_errors_and_merge(tmp_path):
    cfg, err = ddc.load_config(None)
    assert "ddc init" in err and cfg["budget_guard"] is True            # example defaults, so --help/doctor/demo still work
    cfg, err = ddc.load_config(tmp_path / "missing.toml")
    assert "not found" in err
    bad = tmp_path / "bad.toml"
    bad.write_text("budget_usd = [")
    assert "cannot read config" in ddc.load_config(bad)[1]
    ok = tmp_path / "ok.toml"
    ok.write_text('budget_usd = 3.0\n[profiles.8]\ncpu = 8\nmem = "8Gi"\ndisk = "10Gi"\nmax_usd_hr = 0.1\n')
    cfg, err = ddc.load_config(ok)
    assert err is None and cfg["budget_usd"] == 3.0 and cfg["reserve_usd"] == 5.0 and list(cfg["profiles"]) == ["8"]


def test_example_config_has_safe_defaults_and_no_personal_data(tmp_path, monkeypatch):
    cfg, err = ddc.load_config(ddc.data_file("config.example.toml"))
    assert err is None
    assert cfg["budget_guard"] is True and cfg["budget_usd"] == 25.0 and cfg["reserve_usd"] == 5.0
    assert cfg["max_runtime_hours"] == 8 and cfg["worker_runtime_hours"] == 4 and cfg["idle_minutes"] == 45 and cfg["worker_idle_minutes"] == 10
    assert cfg["max_total_leases"] == 4 and cfg["max_workers"] == 4
    assert cfg["state_repo"] == "" and not cfg.get("repos") and not cfg.get("dirs") and cfg["worker_prep_command"] == ""
    text = ddc.data_file("config.example.toml").read_text(encoding="utf-8").lower()
    assert "drill" not in text and "c:/users" not in text
    home = tmp_path / "cdtestuser"                                          # a planted user: nothing of the user's own lands in the starter config
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    monkeypatch.setattr(ddc, "HOME", home)
    monkeypatch.setenv("DDC_CONFIG", str(home / "config.toml"))
    monkeypatch.setattr(ddc, "SECRETS_DIR", home / "sec")
    monkeypatch.setattr(ddc, "KEYS", home / "sec" / "keys")
    ddc.cmd_init(None)
    assert "cdtestuser" not in (home / "config.toml").read_text(encoding="utf-8").lower()


def test_data_files_resolve_in_checkout_and_prefer_packaged_copy(tmp_path, monkeypatch):
    assert ddc.data_file("sdl.yaml.tmpl").is_file() and ddc.data_file("lease").is_dir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sdl.yaml.tmpl").write_text("packaged")
    monkeypatch.setattr(ddc, "PKG", tmp_path)
    assert ddc.data_file("sdl.yaml.tmpl").read_text() == "packaged"


# ------------------------------------------------------------------ ddc init / friendly errors
def test_init_writes_config_and_placeholder_secrets_and_never_overwrites(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg" / "config.toml"
    monkeypatch.setenv("DDC_CONFIG", str(cfg))
    monkeypatch.setattr(ddc, "SECRETS_DIR", tmp_path / "sec")
    monkeypatch.setattr(ddc, "KEYS", tmp_path / "sec" / "keys")
    ddc.cmd_init(None)
    assert ddc.load_config(cfg)[1] is None and cfg.read_text() == ddc.data_file("config.example.toml").read_text()
    sf = tmp_path / "sec" / "secrets.env"
    assert (tmp_path / "sec" / "keys").is_dir()
    assert not ddc.load_secrets()                                          # comments and placeholders only, no live values
    assert all(not ln.strip() or ln.lstrip().startswith("#") for ln in sf.read_text().splitlines())
    if POSIX:
        assert stat.S_IMODE(sf.stat().st_mode) == 0o600
    cfg.write_text("# mine\n")
    sf.write_text("AKASH_API_KEY=keepme\n")
    with pytest.raises(ddc.DdcError, match="not overwriting"):
        ddc.cmd_init(None)
    assert cfg.read_text() == "# mine\n"
    assert sf.read_text() == "AKASH_API_KEY=keepme\n"


def test_init_keeps_existing_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DDC_CONFIG", str(tmp_path / "new" / "config.toml"))
    monkeypatch.setattr(ddc, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(ddc, "KEYS", tmp_path / "keys")
    (tmp_path / "secrets.env").write_text("AKASH_API_KEY=keepme\n")
    ddc.cmd_init(None)
    assert (tmp_path / "secrets.env").read_text() == "AKASH_API_KEY=keepme\n"


@pytest.mark.skipif(not POSIX, reason="POSIX file modes")
def test_secrets_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ddc, "SECRETS_DIR", tmp_path)
    ddc.set_secret("DDC_BACKUP_PASSPHRASE", "x" * 20)
    assert stat.S_IMODE((tmp_path / "secrets.env").stat().st_mode) == 0o600
    (tmp_path / "secrets.env").chmod(0o644)
    ddc.set_secret("OTHER", "y")                                           # rewriting tightens a permissive file
    assert stat.S_IMODE((tmp_path / "secrets.env").stat().st_mode) == 0o600


def test_main_reports_missing_or_broken_config_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "no config found: run `ddc init`, or set DDC_CONFIG")
    assert ddc.main(["status"]) == 1
    assert "ddc init" in capsys.readouterr().out


def test_config_flag_replaces_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DDC_CONFIG", os.environ["DDC_CONFIG"])
    monkeypatch.setattr(ddc, "CONFIG_ERROR", None)
    monkeypatch.setattr(ddc, "CONFIG_FILE", ddc.CONFIG_FILE)
    bad = tmp_path / "bad.toml"
    bad.write_text("x = [")
    assert ddc.main(["--config", str(bad), "status"]) == 1
    assert "cannot read config" in capsys.readouterr().out and ddc.CONFIG_FILE == bad.resolve()


def test_wintest_is_gone(capsys):
    with pytest.raises(SystemExit):
        ddc.main(["wintest"])
    assert not hasattr(ddc, "cmd_wintest")


def test_checkout_shim_and_module_entry_run_help():
    root = Path(ddc.ROOT)
    for argv in ([sys.executable, str(root / "ddc.py"), "--help"], [sys.executable, "-m", "clouddeck.controller", "--help"]):
        cp = subprocess.run(argv, capture_output=True, text=True, cwd=tempfile.gettempdir(), env={**os.environ, "PYTHONPATH": str(root)}, timeout=60)
        assert cp.returncode == 0 and "Disposable Dev Cloud" in cp.stdout, cp.stderr


# ------------------------------------------------------------------ repo paths
def test_repo_path_expands_user_and_names_the_bad_key(tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert ddc.repo_path({"name": "p", "path": "~/proj"}) == tmp_path / "proj"
    with pytest.raises(ddc.DdcError, match=r"q.*path.*C:/Users/x/proj"):       # e.g. a Windows path in a WSL/Linux config
        ddc.repo_path({"name": "q", "path": "C:/Users/x/proj"})


def test_start_fails_on_a_bad_path_before_any_lease_or_api_call(monkeypatch):
    def no_api(*a, **k):
        raise AssertionError("API must not be called")
    monkeypatch.setattr(ddc, "api", no_api)
    ddc.CFG["repos"] = [{"name": "ghost", "path": "/definitely/not/here"}]
    with pytest.raises(ddc.DdcError, match="ghost"):
        ddc.cmd_start(types.SimpleNamespace(profile="32", workers=0, worker_profile=None))


# ------------------------------------------------------------------ empty state_repo = checkpoints disabled
def test_empty_state_repo_disables_checkpoints_cleanly(monkeypatch):
    ddc.CFG["state_repo"] = ""
    assert ddc.state_repo() == "" and ddc.lease_env("1").count('export DDC_STATE_REPO=""') == 1
    for fn in (ddc.verify_checkpoint, lambda: ddc.cmd_checkpoint(None)):
        with pytest.raises(ddc.DdcError, match="state_repo"):
            fn()
    calls = []
    monkeypatch.setattr(ddc, "ssh", lambda d, cmd, **k: calls.append(cmd) or "")
    for force in (False, True):                                           # nothing would be saved: closing the dev box is refused...
        with pytest.raises(ddc.DiscardRefused, match="--discard-workspace"):
            ddc.final_checkpoint({"dseq": "1"}, force=force)
    ddc.final_checkpoint({"dseq": "1"}, discard_workspace=True)           # ...unless the workspace may be destroyed
    assert calls == []                                                    # no checkpoint.sh, no verify, no Claude login pulled to the PC


def test_force_alone_never_destroys_a_workspace_whose_final_checkpoint_failed(monkeypatch):
    """Pure Python, so it runs on Windows too: with a state repo configured but the box unreachable, only --force --discard-workspace may go on."""
    ddc.CFG["state_repo"] = "someone/state"

    def unreachable(*a, **k):
        raise ddc.DdcError("ssh: connection timed out")
    alerts, logs = [], []
    monkeypatch.setattr(ddc, "ssh", unreachable)
    monkeypatch.setattr(ddc, "alert_once", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(ddc, "log", logs.append)
    d = {"dseq": "1", "name": "dev"}
    with pytest.raises(ddc.DdcError, match="timed out") as e:                     # no force: the box stays up and the failure is the answer
        ddc.final_checkpoint(d)
    assert not isinstance(e.value, ddc.DiscardRefused) and len(alerts) == 1
    with pytest.raises(ddc.DiscardRefused, match="--force alone does not destroy"):   # --force alone: still refused, and says what to add
        ddc.final_checkpoint(d, force=True)
    assert not logs
    ddc.final_checkpoint(d, force=True, discard_workspace=True)                    # both flags: closes, with an audit line
    assert any("AUDIT FORCED CLOSE" in m and "WITHOUT a verified checkpoint" in m for m in logs)
    with pytest.raises(ddc.DdcError, match="timed out"):                           # --discard-workspace alone is not --force either
        ddc.final_checkpoint(d, discard_workspace=True)


# ------------------------------------------------------------------ deploy keys
def test_deploy_key_names(monkeypatch):
    ddc.CFG["repos"] = [{"name": "web-app", "path": "/x", "github": "o/web-app", "push": True},
                        {"name": "second", "path": "/y", "github": "o/second", "push": True, "deploy_key": "mykey"},
                        {"name": "ro", "path": "/z", "github": "o/ro", "push": False}]
    assert ddc.repo_deploy_keys() == [("deploy_web-app", "o/web-app"), ("mykey", "o/second")]
    (ddc.KEYS / "desk_deploy").write_text("legacy")                       # an existing legacy key keeps serving the first push repo
    assert ddc.repo_deploy_keys() == [("desk_deploy", "o/web-app"), ("mykey", "o/second")]
    (ddc.KEYS / "deploy_web-app").write_text("new")                       # ...until a per-repo key exists
    assert ddc.repo_deploy_keys()[0][0] == "deploy_web-app"
    ddc.CFG["repos"][1]["deploy_key"] = "../evil"
    with pytest.raises(ddc.DdcError, match="invalid deploy key"):
        ddc.repo_deploy_keys()


def test_provision_dev_ships_state_repo_and_first_push_repo_keys(monkeypatch):
    ddc.CFG["repos"] = [{"name": "web-app", "path": "/x", "github": "o/web-app", "push": True}]
    ddc.CFG["dirs"] = []
    ddc.CFG["state_repo"] = "o/state"
    for k in ("state_deploy", "desk_deploy", "worker", "deploy_web-app"):
        (ddc.KEYS / k).write_text(k)
    for f in ("github_known_hosts", "gitconfig"):
        (ddc.KEYS / f).write_text(f)
    (ddc.SECRETS_DIR / "secrets.env").write_text("DDC_BACKUP_PASSPHRASE=p\n")
    put = []
    monkeypatch.setattr(ddc, "push_lease_scripts", lambda d: None)
    monkeypatch.setattr(ddc, "put_file", lambda d, path, data, **k: put.append(path))
    monkeypatch.setattr(ddc, "ssh", lambda d, cmd, **k: "restored= missing=all\n" if "restore" in cmd else "ok\n")
    monkeypatch.setattr(ddc, "start_session", lambda d: None)
    monkeypatch.setattr(ddc, "seed", lambda d, names: None)
    ddc.provision_dev({"dseq": "1"})
    keys = sorted(p.rsplit("/", 1)[1] for p in put if p.endswith("_key"))
    assert keys == ["repo_deploy_key", "state_deploy_key", "worker_key"]           # lease/setup.sh reads one repo_deploy_key
    assert [p for p in put if p.endswith("repo_deploy_key")] and "desk_deploy" not in " ".join(put)
    ddc.CFG["state_repo"] = ""
    put.clear()
    ddc.provision_dev({"dseq": "1"})
    assert not any(p.endswith("state_deploy_key") for p in put)
    (ddc.KEYS / "worker").unlink()
    with pytest.raises(ddc.DdcError, match="ddc install"):
        ddc.provision_dev({"dseq": "1"})


# ------------------------------------------------------------------ heal hook
def _heal_env(monkeypatch, prep):
    ddc.CFG["worker_prep_command"] = prep
    sent = []
    workers = [{"idx": i, "host": f"h{i}", "port": 22, "name": f"ddc-w0{i}", "dseq": str(i)} for i in (1, 2)]
    monkeypatch.setattr(ddc, "dev_box", lambda: {"dseq": "0"})
    monkeypatch.setattr(ddc, "workers", lambda: workers)
    monkeypatch.setattr(ddc, "ssh_args", lambda w, user="dev", tty=False: ["ssh"])
    monkeypatch.setattr(ddc, "provision_worker", lambda w: None)
    monkeypatch.setattr(ddc.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=b"", stderr=b"", returncode=0))
    monkeypatch.setattr(ddc, "ssh", lambda d, cmd, **k: sent.append(cmd) or "prepped 2\n")
    return sent


def test_heal_without_prep_command_runs_nothing_engine_specific(monkeypatch):
    sent = _heal_env(monkeypatch, "")
    assert ddc.cmd_heal(None) == ["w01", "w02"]
    assert not [c for c in sent if "DIST_HOSTS" in c or "campaign" in c]


def test_heal_prep_command_is_shell_quoted_and_gets_only_healed_hosts(monkeypatch):
    sent = _heal_env(monkeypatch, "echo 'it''s'; touch /tmp/x && sync-repos $DDC_REPOS")
    ddc.cmd_heal(None)
    cmd = next(c for c in sent if "DIST_HOSTS" in c)
    toks = shlex.split(cmd)
    assert toks[:3] == ["DIST_HOSTS=w01 w02", "bash", "-c"]
    assert toks[3] == "echo 'it''s'; touch /tmp/x && sync-repos $DDC_REPOS"      # arrives as ONE argument, never re-parsed


# ------------------------------------------------------------------ rmtree on Python 3.11 and 3.12+
@pytest.mark.parametrize("old", [False, True])
def test_rmtree_removes_read_only_files(tmp_path, monkeypatch, old):
    if old:
        if sys.version_info >= (3, 14):
            pytest.skip("shutil.rmtree(onerror=) is gone")
        monkeypatch.setattr(ddc, "sys", types.SimpleNamespace(version_info=(3, 11, 0)))
    d = tmp_path / "t" / "objects"
    d.mkdir(parents=True)
    f = d / "pack"
    f.write_text("x")
    f.chmod(0o400)
    ddc.rmtree(tmp_path / "t")
    assert not (tmp_path / "t").exists()


# ------------------------------------------------------------------ Windows installer (all subprocess calls mocked)
def _installer(monkeypatch, tmp_path, legacy_task, desktop_has=()):
    monkeypatch.setattr(ddc, "HOME", tmp_path)
    (tmp_path / "Desktop").mkdir()
    for n in desktop_has:
        (tmp_path / "Desktop" / n).write_text("mine")
    calls = []

    def run(argv, **k):
        calls.append(list(argv))
        rc = 0 if (argv[0] != "schtasks" or argv[1] != "/Query" or legacy_task) else 1
        return types.SimpleNamespace(returncode=rc, stdout=b"", stderr=b"")
    monkeypatch.setattr(ddc, "launcher", lambda: ["C:/Py/python.exe", "-m", "clouddeck.controller"])  # the task line must not depend on where the test venv lives
    monkeypatch.setattr(ddc.subprocess, "run", run)
    return calls


def test_install_windows_reports_legacy_task_and_leaves_existing_shims(tmp_path, monkeypatch):
    calls = _installer(monkeypatch, tmp_path, True, desktop_has=("CloudDeck Start.lnk",))
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "ddc.cmd").write_text("existing shim")
    ddc.install_windows()
    assert not [c for c in calls if c[:2] == ["schtasks", "/Create"] or c[:2] == ["schtasks", "/Delete"]]
    assert (tmp_path / "bin" / "ddc.cmd").read_text() == "existing shim"
    assert (tmp_path / "bin" / "claude-cloud.cmd").exists()                # absent shims are still created
    ps = next(c for c in calls if c[0] == "powershell" and "CreateShortcut" in c[-1])[-1]
    assert "CloudDeck Start.lnk" not in ps and "CloudDeck Stop.lnk" in ps and "Drill" not in ps


def test_install_windows_creates_new_task_and_migrate_replaces_legacy(tmp_path, monkeypatch):
    calls = _installer(monkeypatch, tmp_path, False)
    ddc.install_windows()
    create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/TN") + 1] == "CloudDeck-Watchdog" and "pythonw" in create[-1].lower()
    (tmp_path / "m").mkdir()
    calls = _installer(monkeypatch, tmp_path / "m", True)
    ddc.install_windows(migrate=True)
    assert [c[1] for c in calls if c[0] == "schtasks"] == ["/Query", "/Create", "/Delete"]       # new task first, then the legacy one goes
    assert [c for c in calls if c[:2] == ["schtasks", "/Delete"]][0][-1] == "DrillDevCloud-Watchdog"


def test_launcher_uses_checkout_shim_or_module(monkeypatch):
    assert ddc.launcher()[1].endswith("ddc.py")
    monkeypatch.setattr(ddc, "CHECKOUT", None)
    assert ddc.launcher()[1:] == ["-m", "clouddeck.controller"]


# ------------------------------------------------------------------ Akash alert: the old name counts as the same alert
def test_setup_alerts_keeps_legacy_alert(monkeypatch):
    seen = []

    def api(method, path, body=None, **k):
        seen.append((method, path))
        return {"data": [{"id": "c"}] if "channels" in path else [{"name": "drill-dev-cloud low balance"}]}
    monkeypatch.setattr(ddc, "api", api)
    ddc.setup_alerts()
    assert ("POST", "/v1/alerts") not in seen


# ------------------------------------------------------------------ clouddeck CLI
def test_doctor_with_demo_provider_skips_network_checks(capsys, monkeypatch):
    monkeypatch.setattr(ddc, "public_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    for argv in (["-p", "demo", "doctor"], ["doctor", "-p", "demo"]):
        assert cli.main(argv) in (0, 1)
        out = capsys.readouterr().out
        assert "state_repo" in out and "Console API" not in out
    assert "demo" in cli.build_parser().parse_args(["doctor", "-p", "demo"]).provider
    assert cli.build_parser().parse_args(["-p", "demo", "status"]).provider == "demo"


def test_doctor_flags_a_missing_config(capsys, monkeypatch):
    monkeypatch.setattr(ddc, "CONFIG_ERROR", "no config found: run `ddc init`, or set DDC_CONFIG")
    assert cli.main(["-p", "demo", "doctor"]) == 1
    assert "FAIL  config" in capsys.readouterr().out
