"""Offline checks for the money / safety paths of the ddc controller:  python -m pytest -q test_ddc.py  (or python test_ddc.py)"""
import json
import os
import sys
import tempfile
from pathlib import Path

if "pytest" not in sys.modules:  # `python test_ddc.py`: never inherit a real state/secrets/config location from the environment
    import atexit
    import shutil
    _tmp = tempfile.mkdtemp()
    atexit.register(shutil.rmtree, _tmp, True)
    os.environ.update(DDC_STATE_DIR=_tmp, DDC_SECRETS_DIR=_tmp, DDC_CONFIG=str(Path(__file__).parent / "tests" / "fixtures" / "config.toml"))
# under pytest, conftest.py already points all three at temp dirs
from clouddeck import controller as ddc  # noqa: E402


def bid(provider, uact, state="open"):
    return {"bid": {"id": {"provider": provider, "dseq": "1", "gseq": 1, "oseq": 1}, "state": state, "price": {"denom": "uact", "amount": str(uact)}}}


def test_price_conversion_roundtrip():
    assert ddc.uact_block_from_usd_hr(0.90) == 1500
    assert abs(ddc.usd_hr_from_uact_block(1500) - 0.90) < 1e-9


def test_rank_bids_prefers_value_not_raw_price():
    pinfo = {"fast": {"uptime7d": 1, "attributes": [{"key": "capabilities/memory", "value": "ddr5ecc"}]},
             "old": {"uptime7d": 1, "attributes": [{"key": "capabilities/memory", "value": "ddr4"}]},
             "flaky": {"uptime7d": 0.5, "attributes": []}}
    # old is 10% cheaper but 25% slower per thread -> fast wins on $/work
    r = ddc.rank_bids([bid("old", 450), bid("fast", 500), bid("flaky", 10), bid("closed", 1, "closed")], pinfo, {}, {}, 0.95, 4)
    assert [x["provider"] for x in r] == ["fast", "old"]


def test_rank_bids_never_chooses_a_bid_without_a_real_price():
    pinfo = {p: {"uptime7d": 1} for p in ("ok", "zero", "neg", "nan", "inf", "junk", "none")}
    bad = [bid("zero", 0), bid("neg", -5), bid("nan", "nan"), bid("inf", "inf"), bid("junk", "abc"), bid("none", None)]
    bad.append({"bid": {"id": {"provider": "p2"}, "state": "open", "price": {"denom": "uact"}}})           # no amount at all
    assert [x["provider"] for x in ddc.rank_bids(bad + [bid("ok", 900)], pinfo, {}, {}, 0.95, 4)] == ["ok"]
    assert ddc.rank_bids(bad, pinfo, {}, {}, 0.95, 4) == []


def test_rank_bids_measured_speed_overrides_heuristic_and_caps_per_provider():
    pinfo = {"a": {"uptime7d": 1}, "b": {"uptime7d": 1}}
    perf = {"a": {"single_score": 1200}, "b": {"single_score": 600}}
    r = ddc.rank_bids([bid("a", 600), bid("b", 500)], pinfo, perf, {}, 0.95, 4)
    assert r[0]["provider"] == "a"
    r = ddc.rank_bids([bid("a", 600), bid("b", 500)], pinfo, perf, {"a": 4}, 0.95, 4)
    assert [x["provider"] for x in r] == ["b"]
    perf["b"]["bad_until"] = 9e12
    assert ddc.rank_bids([bid("b", 500)], pinfo, perf, {}, 0.95, 4) == []


def test_budget_and_reserve_guards():
    view = {"spent": 40.0, "committed": 20.0, "worst": 60.0, "budget": 85.0}
    ok, _ = ddc.can_afford(view, balance=67.0, new_usd_hr=0.9, new_hours=8, budget=85, reserve=15)
    assert ok  # 60 + 7.2 <= 85 and 67 - 20 - 7.2 >= 15
    ok, why = ddc.can_afford(view, balance=67.0, new_usd_hr=0.9 * 12, new_hours=4, budget=85, reserve=15)
    assert not ok and "budget" in why  # 60 + 43.2 > 85
    ok, why = ddc.can_afford({"spent": 0, "committed": 40, "worst": 40, "budget": 85}, 60.0, 0.9, 8, 85, 15)
    assert not ok and "reserve" in why  # 60 - 40 - 7.2 < 15


def test_running_cost_uses_max_of_escrow_and_elapsed():
    t = 1_000_000.0
    d = {"usd_hr": 0.5, "lease_at": t - 2 * 3600, "limit_h": 8, "transferred_usd": 0.2}
    spent, committed = ddc.dep_running_cost(d, t)
    assert abs(spent - 1.0) < 1e-9 and abs(committed - 3.0) < 1e-9


def test_never_closes_foreign_deployments():
    ddc.write_json(ddc.STATE_FILE, {"deps": {"111": {"ours": True, "name": "ddc-dev-32-x"}}})
    for dseq, det in [("1000000000001", {"name": "other-service+db+web"}),  # someone else's deployment
                      ("111", {"name": "renamed-in-console"})]:  # recorded but renamed in console -> refuse
        try:
            ddc.assert_ours(dseq, det)
        except ddc.DdcError:
            continue
        raise AssertionError(f"assert_ours accepted {dseq}")
    ddc.assert_ours("111", {"name": "ddc-dev-32-x"})  # the frozen "ddc-" prefix: leases created by older versions stay closable


def test_secret_detection():
    kp = json.dumps(list(range(64))).encode()
    assert ddc.secret_hits("wallets/main.json", b"{}", [])
    assert ddc.secret_hits("x/id.json", kp, [])
    assert ddc.secret_hits(".env", b"A=1", []) and not ddc.secret_hits(".env.example", b"A=", [])
    assert ddc.secret_hits("notes.md", b"token ghp_" + b"a" * 36, [])
    assert ddc.secret_hits("a.ts", b"const k = 'SUPERSECRETVALUE1'", [b"SUPERSECRETVALUE1"])
    assert not ddc.secret_hits("src/wallet/keypair.ts", b"export function loadKeypair() {}", [])


def test_sdl_has_only_public_material():
    keys = ddc.KEYS
    keys.mkdir(parents=True, exist_ok=True)
    (keys / "access.pub").write_text("ssh-ed25519 AAAAPUBLIC access")
    (keys / "worker.pub").write_text("ssh-ed25519 AAAAWORKER worker")
    s = ddc.render_sdl("worker", "32")
    assert "units: 32" in s and "size: 64Gi" in s and "amount: 1500" in s and "PRIVATE" not in s


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
