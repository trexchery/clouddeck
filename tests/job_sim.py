"""Old flow vs new flow on a virtual clock:  `python tests/job_sim.py [--json]`  (the same code runs in tests/test_job_runner.py).

The same JobRunner is run twice against the stubbed Akash API / box (tests/job_fakes.py), in a throw-away state dir:
  old = `overlap=False` (lease at once, prep starts after boot, setup after prep, retrieval at the end, manual close lag)
  new = default flow (bid early / lease late, prep overlaps boot, setup starts at sshd, streamed retrieval, automatic close)

Every input below is ASSUMED (a synthetic, round-number scenario; nothing here was measured on a real lease).
Every output is ESTIMATED: a model driven by those assumptions, never a measurement of a real lease.
"""
from __future__ import annotations

import os

if __name__ == "__main__":   # run as a script: the controller reads its directories at import time, so point them at a temp dir first (never real state)
    import atexit
    import shutil
    import sys
    import tempfile
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))   # a plain checkout: `clouddeck` is importable without installing it

    _root = _P(tempfile.mkdtemp(prefix="clouddeck-sim-"))
    atexit.register(shutil.rmtree, _root, True)
    for _d in ("state", "secrets/keys"):
        (_root / _d).mkdir(parents=True)
    shutil.copy(_P(__file__).parent / "fixtures" / "config.toml", _root / "config.toml")
    os.environ.update(DDC_STATE_DIR=str(_root / "state"), DDC_SECRETS_DIR=str(_root / "secrets"), DDC_CONFIG=str(_root / "config.toml"))

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from job_fakes import FakeAkash, FakeRemote, TimerPrep, install

from clouddeck.engine import ddc
from clouddeck.job import spec as specmod
from clouddeck.job.clock import FakeClock
from clouddeck.job.runner import JobRunner
from clouddeck.job.telemetry import format_summary

# ---- ASSUMED inputs: a synthetic scenario with round numbers, not taken from any real lease
USD_HR = 2.0                  # lease price
BOOT_S = 60                   # lease -> sshd reachable, stock image (pull + apt)
PREP_IDLE_S = 900             # local preparation time (the old flow leases first and pays for it)
SETUP_S = 200                 # tool install + downloads
RUN_S = [300, 300, 1200]      # the productive runs
GAP_S = [60, 30, 120]         # time before each run in the old flow (setup/previous run end -> next run queued by hand)
TAIL_S = 240                  # last run end -> lease closed in the old flow (pull, verify, look, manual close)
BID_DELAY_S = 25              # first bids appear
LEASE_CALL_S = 16             # manifest delivery
OLD_BID_WAIT_S = 56           # old policy: first poll after 13 x 4 s when < 3 bids
NEW_BID_WAIT_S = 20
APT_SAVED_S = 30              # share of the boot that a prebuilt image skips (apt-get update/install)
STOCK_IMAGE = "example/stock-image:1"
PREBUILT_IMAGE = "ghcr.io/example/ddc-base:1"


def _spec(base: Path, steps: list[dict[str, Any]], prep_s: float | None, estimate: float | None, image_prebuilt: bool, bid_wait: float) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "name": "sim", "runtime_hours": 6, "max_cost_usd": 20, "estimated_runtime_min": 150, "artifacts_dir": "got", "bid_wait_s": bid_wait,
        "setup_estimate_s": SETUP_S, "idle_abort_s": 3600, "max_prep_wait_s": 7200, "poll_s": 10,
        "hardware": {"cpu": 16, "memory": "96Gi", "disk": "200Gi", "max_usd_hr": 3.2, "gpu": {"vendor": "vendor-a", "models": ["model-a:80Gi"]}},
        "image": ({"prebuilt": PREBUILT_IMAGE, "stock": STOCK_IMAGE, "prebuilt_timeout_s": 600} if image_prebuilt else {"stock": STOCK_IMAGE}),
        "inputs": ["ref.bin"] if prep_s else [], "chunk_mb": 1,
        "cache": {"item": [{"name": "models", "dest": "models/m.bin", "url": "https://example.com/m.bin", "sha256": "a" * 64, "size_gb": 10}]},
        "step": steps}
    if prep_s:
        raw["prep"] = {"cmd": "local prep", "estimate_s": estimate or 0, "produces": ["ref.bin"]}
    return specmod.normalize(raw, base)


def _fresh_world(mp: Any, clock: FakeClock, akash: FakeAkash) -> None:
    """Empty state/ledger/history of the previous flow (each flow is a clean account), then wire in the fakes."""
    for d in (ddc.STATE_DIR, ddc.SECRETS_DIR):
        if Path(tempfile.gettempdir()).resolve() not in d.resolve().parents:   # same guard as tests/conftest.py: never wipe a real directory
            raise SystemExit(f"refusing to wipe {d}: not under the temp dir")
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    install(mp, clock, akash)
    ddc.CFG.update(budget_usd=1000.0, max_total_leases=4, max_price_usd_hr=5.0)   # the sim prices a GPU at ~$2/h: above the default $2/h per-lease ceiling


def run_flow(*, old: bool, steps: list[tuple[str, float, bool]], prep_s: float, prep_estimate: float | None, boot_stock: float = BOOT_S,
             apt_saved: float = APT_SAVED_S, close_lag_s: float = 0.0, warm: bool = True) -> dict[str, Any]:
    """steps = [(name, duration_s, productive)] run strictly in sequence after the model download. Returns the telemetry summary."""
    try:
        import pytest  # only for its MonkeyPatch (the fakes are wired in with it): the dev extra, `pip install -e .[dev]`
    except ImportError:
        raise SystemExit("the simulation needs pytest: pip install -e .[dev]") from None
    clock = FakeClock()
    akash = FakeAkash(clock, bid_delay_s=BID_DELAY_S, port_delay_s=10, pull_s=10, lease_latency_s=LEASE_CALL_S, balance_usd=10_000.0,
                      providers=[{"owner": "akash1sim", "usd_hr": USD_HR, "uptime7d": 0.99, "gpuModels": [{"model": "model-a", "ram": "80Gi"}]}],
                      boot_s={"ghcr.io/example/ddc-base": max(10.0, boot_stock - apt_saved), "default": boot_stock})
    with tempfile.TemporaryDirectory() as td, pytest.MonkeyPatch.context() as inner:
        _fresh_world(inner, clock, akash)
        base = Path(td)
        steps_raw = [{"name": n, "run": "x", "productive": p, **({"artifacts": ["out/*.bin"]} if i == len(steps) - 1 else {})} for i, (n, _, p) in enumerate(steps)]
        spec = _spec(base, steps_raw, prep_s, prep_estimate, image_prebuilt=not old, bid_wait=OLD_BID_WAIT_S if old else NEW_BID_WAIT_S)
        dur = {"cache-models": SETUP_S, **{n: d for n, d, _ in steps}}

        def factory(host, port):
            r = FakeRemote(clock, akash, durations=dur, produces={steps[-1][0]: {"out/result.bin": b"x" * 2_000_000}}, down_bps=10e6)
            r.dseq = list(akash.deps)[-1]
            return r

        runner = JobRunner(spec, clock=clock, remote_factory=factory, overlap=not old, prefer_prebuilt=not old, install_signals=False, log=lambda m: None,
                           prep_factory=lambda s: TimerPrep(clock, prep_s, {base / "ref.bin": b"data"}))
        if warm and not old:  # an earlier run already recorded this provider's measured boot for the prebuilt image
            runner.hist.record("akash1sim", PREBUILT_IMAGE, ok=True, boot_s=max(10.0, boot_stock - apt_saved))
        if close_lag_s:
            orig = runner._close

            def lagged():
                if not runner.closed:
                    clock.sleep(close_lag_s)  # a human checks the output and closes by hand
                orig()

            runner._close = lagged
        t = runner.run()
        s, m = t["summary"], t["marks_epoch"]
        return {"billed_s": round(s["billed_s"]), "productive_s": round(s["productive_s"]), "productive_pct": s["productive_pct"],
                "idle_s": round(s["idle_overhead_s"]), "cost_usd": round(s["billed_usd_est"], 2), "create_to_lease_s": round(m["lease"] - m["create"]),
                "boot_s": round(m["ssh_ready"] - m["lease"]), "job_done_after_create_s": round(m["job_done"] - m["create"]), "parts": s["parts"],
                "summary_text": format_summary(t)}


def scenarios() -> dict[str, Any]:
    out: dict[str, Any] = {}
    # A. a multi-run session: several runs with human decision gaps between them in the old flow.
    seq_old = [x for i, g in enumerate(GAP_S) for x in ((f"g{i + 1:02d}", g, False), (f"r{i + 1:02d}", RUN_S[i], True))]
    seq_new = seq_old[1:]  # the first "start the run by hand" gap disappears with stream-start
    old = run_flow(old=True, steps=seq_old, prep_s=PREP_IDLE_S, prep_estimate=None, close_lag_s=TAIL_S - 3)
    out["A_session"] = {"old": old, "new": run_flow(old=False, steps=seq_new, prep_s=PREP_IDLE_S, prep_estimate=PREP_IDLE_S)}
    # sensitivity of the new flow
    sens = {}
    for label, kw in {"first run, no boot history (cold_boot_s=180 default)": {"warm": False},
                      "prep ETA 300 s too optimistic": {"prep_estimate": PREP_IDLE_S - 300}, "prep ETA 300 s too pessimistic": {"prep_estimate": PREP_IDLE_S + 300},
                      "no prep ETA given (lease after prep done)": {"prep_estimate": None}, "prebuilt image saves 0 s of apt": {"apt_saved": 0},
                      "prebuilt image saves 15 s of apt": {"apt_saved": 15}}.items():
        sens[label] = run_flow(old=False, steps=seq_new, prep_s=PREP_IDLE_S, **{"prep_estimate": PREP_IDLE_S, **kw})
    out["A_sensitivity_new"] = sens
    # B. one clean single job. prep 600 s is ASSUMED.
    one = [("work", RUN_S[-1], True)]
    out["B_single_job"] = {"old": run_flow(old=True, steps=one, prep_s=600, prep_estimate=None, close_lag_s=TAIL_S - 3),
                           "new": run_flow(old=False, steps=one, prep_s=600, prep_estimate=600)}
    return out


def _row(name: str, r: dict[str, Any]) -> str:
    return f"| {name} | {r['billed_s']} | {r['productive_s']} | {r['productive_pct']} % | {r['idle_s']} | ${r['cost_usd']:.2f} | {r['job_done_after_create_s']} |"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = scenarios()
    if a.json:
        print(json.dumps(r, indent=1))
        return 0
    hdr = f"| flow | billed s | productive s | productive | non-productive s | cost @ ${USD_HR}/h | job done, s after create |\n|---|---|---|---|---|---|---|"
    A, B = r["A_session"], r["B_single_job"]
    print("ESTIMATED (a model driven by assumed, synthetic inputs; see the header of tests/job_sim.py)\n")
    print("A. Multi-run session")
    print(hdr)
    print(_row("old", A["old"]))
    print(_row("new", A["new"]))
    sav = A["old"]["billed_s"] - A["new"]["billed_s"]
    print(f"\nA saving: {sav} s billed = ${sav / 3600 * USD_HR:.2f} ({100 * sav / A['old']['billed_s']:.1f} % of the old bill); the gaps between runs are unchanged by design\n")
    print("A sensitivity (new flow)")
    print(hdr)
    for k, v in r["A_sensitivity_new"].items():
        print(_row(k, v))
    print("\nB. Single job (prep 600 s ASSUMED)")
    print(hdr)
    print(_row("old", B["old"]))
    print(_row("new", B["new"]))
    sb = B["old"]["billed_s"] - B["new"]["billed_s"]
    print(f"\nB saving: {sb} s billed = ${sb / 3600 * USD_HR:.2f} ({100 * sb / B['old']['billed_s']:.1f} % of the old bill)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
