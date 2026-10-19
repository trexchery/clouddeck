"""Spending safety: guard on by default, lease-count caps, hard ceilings that hold with the guard off, config validation, SDL/shell
injection, committed-worst-case accounting. Everything runs on the in-memory fake (tests/fake_akash.py)."""
import math
import threading
import tomllib

import pytest

from clouddeck.engine import ddc


def deps(**by_dseq):
    ddc.write_json(ddc.STATE_FILE, {"deps": by_dseq})


def rec(role="worker", status="ready", usd_hr=0.3, **kw):
    return {"ours": True, "name": f"ddc-{role}-32-x", "role": role, "status": status, "usd_hr": usd_hr, "lease_at": ddc.now(),
            "limit_h": 8, "max_usd_hr": 0.9, "provider": "p", "host": "h.example", "port": 22, "profile": "32", "idx": 1, **kw}


# ------------------------------------------------------------------ defaults
def test_code_defaults_match_the_example_config_and_are_safe(tmp_path):
    ex = tomllib.loads(ddc.data_file("config.example.toml").read_text(encoding="utf-8"))
    assert (ex["budget_guard"], ex["budget_usd"], ex["reserve_usd"]) == (True, 25, 5)
    user = tmp_path / "c.toml"                                   # a user config that sets none of the spending keys
    user.write_text('[profiles.x]\ncpu = 1\nmem = "1Gi"\ndisk = "1Gi"\nmax_usd_hr = 0.1\n')
    cfg, err = ddc.load_config(user)
    assert err is None and cfg["budget_guard"] is True and cfg["budget_usd"] == 25 and cfg["reserve_usd"] == 5
    assert cfg["max_total_leases"] == 4 and cfg["max_workers"] == 4
    assert ddc.config_problems(ex) == [] and ddc.config_problems(cfg) == []
    assert "NEW" not in ddc.data_file("config.example.toml").read_text(encoding="utf-8")


def test_budget_guard_blocks_over_budget_and_reserve_breach(fake):
    ddc.CFG.update(budget_usd=5.0)                               # 0.9 $/h cap x 8 h = 7.2 > 5
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        ddc.check_budget("32", 1, 8, role="dev")
    ddc.CFG.update(budget_usd=50.0, reserve_usd=5.0)
    fake.balance = 10.0                                          # 10 - 7.2 < reserve 5
    with pytest.raises(ddc.LimitExceeded, match="reserve"):
        ddc.check_budget("32", 1, 8, role="dev")
    assert not fake.deployments


def test_budget_guard_fails_closed_when_the_balance_api_is_down(fake):
    fake.fail_next("GET /v1/balances", "error")
    with pytest.raises(ddc.DdcError):
        ddc.create_box("dev", "32")
    assert not [c for c in fake.calls if c == ("POST", "/v1/deployments")]       # nothing was created


def test_string_false_for_the_guard_is_a_config_error_not_silently_off(fake):
    ddc.CFG["budget_guard"] = "false"
    with pytest.raises(ddc.DdcError, match="budget_guard must be true or false"):
        ddc.check_budget("32", 1, 8)


# ------------------------------------------------------------------ lease-count caps (enforced, not just documented)
def test_max_total_leases_is_enforced_including_pending_creations(fake):
    ddc.CFG["max_total_leases"] = 2
    deps(a=rec("dev"), b=rec("worker", idx=1))
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases = 2"):
        ddc.create_box("worker", "32", 2)
    ddc.write_json(ddc.STATE_FILE, {"deps": {"a": rec("dev")}, "pending": {"ddc-w01-32-x": {"role": "worker", "max_usd_hr": 0.9, "limit_h": 4, "created_at": ddc.now()}}})
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases"):                # an unresolved creation counts as alive
        ddc.check_budget("32", 1, 4, role="worker")
    assert not fake.deployments


def test_max_workers_is_enforced(fake):
    ddc.CFG.update(max_workers=1, max_total_leases=8)
    deps(a=rec("dev"), b=rec("worker", idx=1))
    with pytest.raises(ddc.LimitExceeded, match="max_workers = 1"):
        ddc.create_box("worker", "32", 2, limit_h=4)
    ddc.check_budget("32", 1, 4, role="dev")                      # the cap is about workers only


def test_scale_goes_through_the_same_gate_and_never_exceeds_the_cap(fake):
    ddc.CFG.update(max_workers=2, max_total_leases=8, budget_usd=500.0)
    deps(dev=rec("dev", host="dev.example"))
    with pytest.raises(ddc.LimitExceeded, match="max_workers = 2"):
        ddc.cmd_scale(type("A", (), {"n": 3, "profile": "32", "explore": False})())
    assert not fake.deployments                                  # refused before the first lease, not after two
    ddc.cmd_scale(type("A", (), {"n": 2, "profile": "32", "explore": False})())
    assert len(fake.open_dseqs()) == 2 and len(ddc.workers()) == 2


def test_concurrent_creates_cannot_race_past_the_cap(fake):
    ddc.CFG.update(max_workers=2, max_total_leases=8, budget_usd=500.0)
    results, lock = [], threading.Lock()

    def go(i):
        try:
            ddc.create_box("worker", "32", i, {}, limit_h=4)
            out = "ok"
        except ddc.LimitExceeded:
            out = "refused"
        with lock:
            results.append(out)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(1, 6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["ok", "ok", "refused", "refused", "refused"] and len(fake.open_dseqs()) == 2


# ------------------------------------------------------------------ hard ceilings hold with the guard off
@pytest.fixture
def guard_off(fake):
    ddc.CFG["budget_guard"] = False
    return fake


def test_hard_lease_ceiling_with_guard_off(guard_off, capsys):
    ddc.CFG["max_total_leases"] = 4
    deps(**{str(i): rec("worker", idx=i) for i in range(1, 5)})
    with pytest.raises(ddc.LimitExceeded, match="max_total_leases"):
        ddc.check_budget("32", 1, 4, role="worker")
    # raising the config cap above the hard ceiling is a config error, not a way around it
    ddc.CFG["max_total_leases"] = 50
    with pytest.raises(ddc.DdcError, match="hard ceiling"):
        ddc.check_budget("32", 1, 4, role="worker")
    assert not guard_off.deployments


def test_hard_price_runtime_and_scale_ceilings_with_guard_off(guard_off):
    ddc.CFG["profiles"]["big"] = {"cpu": 64, "mem": "128Gi", "disk": "100Gi", "max_usd_hr": 25.0}
    with pytest.raises(ddc.DdcError, match="hard ceiling"):
        ddc.create_box("worker", "big", 1)
    ddc.CFG["max_runtime_hours"] = 48
    with pytest.raises(ddc.DdcError, match="max_runtime_hours = 48 is above the hard ceiling"):
        ddc.check_budget("32", 1, 48)
    ddc.CFG["max_runtime_hours"] = 8
    with pytest.raises(ddc.LimitExceeded, match="runtime limit"):
        ddc.check_budget("32", 1, 30)                              # per-call hours above the ceiling, config untouched
    with pytest.raises(ddc.LimitExceeded, match="one call"):
        ddc.check_budget("32", ddc.HARD_MAX_SCALE + 1, 4, role="worker")
    for n in (-1, 99):
        with pytest.raises(ddc.DdcError, match="cannot scale"):
            ddc.cmd_scale(type("A", (), {"n": n, "profile": "32"})())
    assert not guard_off.deployments


def test_guard_off_alone_does_not_skip_the_hard_checks_but_logs_the_worst_case(guard_off, capsys):
    deps(dev=rec("dev"))
    ddc.CFG.update(max_workers=4, max_total_leases=4)
    ddc.check_budget("32", 1, 4, role="worker")                   # within ceilings: allowed
    assert "budget guard OFF" in capsys.readouterr().out


def test_unsafe_disable_limits_is_the_only_way_past_the_ceilings_and_it_is_loud(guard_off, capsys, monkeypatch):
    ddc.CFG.update(unsafe_disable_limits=True, max_total_leases=20, max_workers=20)
    ddc.CFG["profiles"]["big"] = {"cpu": 64, "mem": "128Gi", "disk": "100Gi", "max_usd_hr": 25.0}
    assert ddc.limits_unsafe() and ddc.config_problems() == []
    ddc.check_budget("big", 1, 48, role="worker")
    assert capsys.readouterr().out.count("unsafe_disable_limits = true") >= 1
    ddc.CFG["unsafe_disable_limits"] = "yes"                      # only a real boolean counts
    assert not ddc.limits_unsafe()
    ddc.CFG["unsafe_disable_limits"] = True
    ddc.CFG.pop("repos", None)
    monkeypatch.setattr(ddc, "cmd_status", lambda a: 0)
    assert ddc.main(["status"]) == 0
    assert "unsafe_disable_limits = true" in capsys.readouterr().out         # printed on every run, not only on provisioning


# ------------------------------------------------------------------ config validation
@pytest.mark.parametrize("key,bad", [("budget_usd", float("nan")), ("budget_usd", float("inf")), ("budget_usd", -1), ("budget_usd", "25"),
                                     ("reserve_usd", -5), ("reserve_usd", True), ("max_runtime_hours", 0), ("max_runtime_hours", 2.5),
                                     ("idle_minutes", 0), ("checkpoint_minutes", -3), ("max_workers", -1), ("max_total_leases", 0),
                                     ("min_uptime_7d", 2), ("image", "node:22\n privileged: true"), ("image", "x y"), ("image", ""),
                                     ("state_repo", "not a repo"), ("busy_regex", "a\nb"), ("claude_code_version", "latest"),
                                     ("install_claude_code", "yes"), ("push_claude_credentials", 1), ("sync_excludes", ["a;b"])])
def test_config_values_are_validated(key, bad):
    assert any(key in p or "image" in p for p in ddc.config_problems({**ddc.CFG, key: bad})), key


def test_nan_budget_cannot_disable_the_guard(fake):
    ddc.CFG["budget_usd"] = float("nan")
    with pytest.raises(ddc.DdcError, match="budget_usd"):
        ddc.check_budget("32", 1, 8)


@pytest.mark.parametrize("field,bad", [("mem", "64Gi\n evil: true"), ("mem", "lots"), ("disk", "100GB"), ("cpu", 0), ("cpu", 1000), ("cpu", "32"),
                                       ("max_usd_hr", 0), ("max_usd_hr", float("nan")), ("max_usd_hr", 99), ("gpu", 99), ("gpu_model", "a100\n x: y")])
def test_profile_fields_cannot_inject_yaml_or_exceed_limits(field, bad, fake):
    ddc.CFG["profiles"]["evil"] = {**ddc.CFG["profiles"]["32"], field: bad}
    with pytest.raises(ddc.DdcError):
        ddc.render_sdl("dev", "evil")
    with pytest.raises(ddc.DdcError):
        ddc.create_box("dev", "evil")
    assert not fake.deployments


def test_image_cannot_inject_yaml_into_the_sdl():
    ddc.CFG["image"] = "node:22\n    privileged: true"
    with pytest.raises(ddc.DdcError, match="image"):
        ddc.render_sdl("dev", "32")


def test_project_names_and_github_slugs_are_validated(tmp_path):
    for bad in ("x; touch /tmp/pwned #", "", "a b", "../up", "a/b", "-rf"):
        assert ddc.config_problems({**ddc.CFG, "repos": [{"name": bad, "path": str(tmp_path)}]}), bad
    assert ddc.config_problems({**ddc.CFG, "repos": [{"name": "ok", "path": str(tmp_path), "github": "no slash"}]})
    assert ddc.config_problems({**ddc.CFG, "repos": [{"name": "dup", "path": "/a"}], "dirs": [{"name": "dup", "path": "/b"}]})
    assert not ddc.config_problems({**ddc.CFG, "repos": [{"name": "my-app_2.0", "path": str(tmp_path), "github": "o/my-app"}]})


def test_existing_custom_profiles_keep_loading_and_one_bad_profile_does_not_block_the_others(fake):
    ddc.write_json(ddc.STATE_DIR / "custom_profiles.json", {"x32-64-100": {"cpu": 32, "mem": "64Gi", "disk": "100Gi", "max_usd_hr": 0.9},
                                                            "bad": {"cpu": 32, "mem": "64Gi\nx", "disk": "100Gi", "max_usd_hr": 0.9}})
    ddc.load_custom_profiles()
    assert ddc.config_problems(profile="x32-64-100") == []
    ddc.render_sdl("worker", "x32-64-100")
    assert ddc.config_problems(profile="bad") and ddc.config_problems()            # reported when asked for all
    ddc.check_budget("x32-64-100", 1, 4, role="worker")                             # ...but "bad" does not block "x32-64-100"


# ------------------------------------------------------------------ committed worst-case accounting
def test_committed_worst_case_counts_running_leases_and_unknown_creations(fake):
    t = ddc.now()
    ddc.write_json(ddc.STATE_FILE, {"deps": {"1": rec("dev", usd_hr=0.5, lease_at=t - 2 * 3600, limit_h=8)},
                                    "pending": {"ddc-w01-32-aa": {"role": "worker", "max_usd_hr": 0.9, "limit_h": 4, "created_at": t}}})
    v = ddc.budget_view()
    assert v["committed"] == pytest.approx(0.5 * 6 + 0.9 * 4) and v["spent"] == pytest.approx(1.0)
    assert math.isclose(v["worst"], v["spent"] + v["committed"])
    ddc.CFG["budget_usd"] = 10.0                                    # worst 1.0 + 6.6 = 7.6; one more worker (3.6) would make 11.2
    with pytest.raises(ddc.LimitExceeded, match="budget"):
        ddc.check_budget("32", 1, 4, role="worker")


def test_closed_lease_is_booked_at_least_at_its_running_estimate(fake):
    box = ddc.create_box("worker", "32", 1, {}, limit_h=4)
    fake.clock += 3600
    d = fake.deployments[box["dseq"]]
    d["usd_hr"] = 0.0                                               # escrow settled lazily: transferred far below elapsed x rate
    cost = ddc.close_box(box["dseq"])
    assert cost == pytest.approx(0.3, abs=0.01) and ddc.load_state()["deps"][box["dseq"]]["cost_usd"] == pytest.approx(cost)
