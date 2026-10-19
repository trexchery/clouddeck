import io
import json
import types

import pytest

from clouddeck import cli
from clouddeck.engine import ddc

KEY = "ak_test_" + "0123456789abcdef"
REPLACEMENT_KEY = "ak_other_" + "9876543210"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Nothing real runs: keygen writes fake files, the scheduler/balance are stubbed and recorded."""
    calls = []
    monkeypatch.setattr(ddc, "SECRETS_DIR", tmp_path / "sec")
    monkeypatch.setattr(ddc, "KEYS", tmp_path / "sec" / "keys")
    monkeypatch.setattr(ddc, "balance_usd", lambda: 12.5)
    monkeypatch.setattr(ddc, "watchdog_installed", lambda: False)
    monkeypatch.setattr(ddc, "SSH", "python")   # any binary that exists: the ssh lookup is shared with the controller

    def run(argv, **k):
        calls.append(list(argv))
        if argv[0] == ddc.SSH_KEYGEN:
            (ddc.KEYS / f"{argv[argv.index('-C') + 1][4:]}").write_text("PRIVATE")
        return types.SimpleNamespace(returncode=1 if argv[:2] == ["schtasks", "/Query"] else 0, stdout=b"", stderr=b"")   # no legacy task
    monkeypatch.setattr(ddc.subprocess, "run", run)
    return calls


def setup(monkeypatch, capsys, *argv, stdin=KEY):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = ddc.main(["setup", *argv])
    return rc, capsys.readouterr()


def key_lines(tmp_path):
    return [ln for ln in (tmp_path / "sec" / "secrets.env").read_text().splitlines() if ln.startswith("AKASH_API_KEY")]


def test_key_stored_once_replaced_never_printed(world, monkeypatch, capsys, tmp_path):
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", "--json")
    assert key_lines(tmp_path) == [f"AKASH_API_KEY={KEY}"] and rc == 0
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", "--json", stdin=REPLACEMENT_KEY + "\n")
    assert key_lines(tmp_path) == [f"AKASH_API_KEY={REPLACEMENT_KEY}"]
    assert KEY not in out.out + out.err and REPLACEMENT_KEY not in out.out + out.err


@pytest.mark.parametrize("bad", ["", "  \n", "abc", "two words here_1234", "line1_abcdefgh\nline2_abcdefgh"])
def test_bad_key_refused(world, monkeypatch, capsys, tmp_path, bad):
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", stdin=bad)
    assert rc == 1 and not (tmp_path / "sec" / "secrets.env").exists()   # refused before anything is written


def test_access_key_generated_when_missing_and_kept_when_present(world, monkeypatch, capsys):
    setup(monkeypatch, capsys, "--api-key-stdin")
    keygens = [c for c in world if c[0] == ddc.SSH_KEYGEN and "-t" in c]
    assert len(keygens) == 1 and keygens[0][keygens[0].index("-C") + 1] == "ddc-access" and (ddc.KEYS / "access").is_file()
    setup(monkeypatch, capsys)
    assert len([c for c in world if c[0] == ddc.SSH_KEYGEN and "-t" in c]) == 1


def test_json_shape_ready_exit_0(world, monkeypatch, capsys):
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", "--json")
    r = json.loads(out.out)
    assert rc == 0 and r["ready_for_jobs"] is True and r["api_key_set"] and r["api_key_valid"] is True and r["balance_usd"] == 12.5
    assert set(r) == {"config_file", "secrets_file", "api_key_set", "api_key_valid", "balance_usd", "access_key", "ssh", "watchdog", "ready_for_jobs", "next_steps"}
    assert r["access_key"] is True and r["ssh"] is True and r["watchdog"] is False and r["next_steps"][-1] == "run: ddc job hardware --available" and "ddc setup --watchdog" in r["next_steps"][0]


def test_not_ready_without_key_exit_2(world, monkeypatch, capsys):
    rc, out = setup(monkeypatch, capsys, "--json")
    r = json.loads(out.out)
    assert rc == 2 and r["api_key_set"] is False and r["api_key_valid"] is None and r["balance_usd"] is None and "--api-key-stdin" in r["next_steps"][0]


def test_rejected_key_is_not_ready(world, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "balance_usd", lambda: ddc.die("HTTP 401"))
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", "--json")
    assert rc == 2 and json.loads(out.out)["api_key_valid"] is False


def test_watchdog_registers_only_the_windows_task(world, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "WIN", True)
    monkeypatch.setattr(ddc, "launcher", lambda: ["C:/Py/python.exe", "-m", "clouddeck.controller"])
    monkeypatch.setattr(ddc, "owner_only", lambda p: None)
    setup(monkeypatch, capsys, "--watchdog")
    progs = [c[0] for c in world]
    assert ["schtasks", "/Create"] in [c[:2] for c in world] and "powershell" not in progs
    create = next(c for c in world if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/TN") + 1] == ddc.TASK


def _doctor(monkeypatch, capsys, repo):
    monkeypatch.setattr(ddc, "GPG", "no-such-gpg-binary")
    monkeypatch.setitem(ddc.CFG, "state_repo", repo)
    monkeypatch.setattr(ddc, "public_get", lambda *a, **k: [])
    monkeypatch.setattr(ddc.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    cli.main(["doctor"])
    return capsys.readouterr().out


def test_doctor_gpg_and_passphrase_warn_without_state_repo(monkeypatch, capsys):
    out = _doctor(monkeypatch, capsys, "")
    assert "WARN  gpg" in out and "WARN  DDC_BACKUP_PASSPHRASE" in out
    out = _doctor(monkeypatch, capsys, "me/state")
    assert "FAIL  gpg" in out and "FAIL  DDC_BACKUP_PASSPHRASE" in out


def test_setup_uses_an_existing_config_instead_of_shadowing_it(world, monkeypatch, capsys, tmp_path):
    legacy = tmp_path / "legacy" / "config.toml"
    legacy.parent.mkdir()
    legacy.write_text("budget_usd = 3.0\n")
    monkeypatch.setattr(ddc, "CONFIG_FILE", legacy)
    monkeypatch.setattr(ddc, "default_config_path", lambda: tmp_path / "userdir" / "config.toml")
    setup(monkeypatch, capsys, "--api-key-stdin", "--json")
    assert not (tmp_path / "userdir" / "config.toml").exists() and legacy.read_text() == "budget_usd = 3.0\n"


def test_unreachable_api_is_not_reported_as_a_bad_key(world, monkeypatch, capsys):
    monkeypatch.setattr(ddc, "balance_usd", lambda: ddc.die("GET /v1/balances: timed out"))
    rc, out = setup(monkeypatch, capsys, "--api-key-stdin", "--json")
    j = json.loads(out.out)
    assert rc == 2 and j["api_key_valid"] is None and any("could not reach" in s for s in j["next_steps"])
