"""ddc job  (also: python -m clouddeck.job)

  run SPEC      preflight, then bid early / lease late, run the steps, retrieve + verify artifacts, close + verify
  check SPEC    preflight only (add --offline to skip the network checks)
  sdl SPEC      print the SDL that would be deployed
  hardware      public GPU price/availability table (read-only)
  report [JOB]  print the latest telemetry summary
  close JOB     verified close of a job's lease (e.g. after --keep or a killed run)

Every lease is created through the controller's spending gate (budget guard, lease caps, price and runtime ceilings) and closed only
through its ownership-checked close. See docs/JOBS.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clouddeck import controller as ddc

from . import preflight
from .clock import RealClock
from .runner import JobError, JobRunner
from .spec import NAME_RE, SpecError, image_kinds, load_spec, render_sdl
from .telemetry import format_summary


def _checks(spec, offline: bool) -> bool:
    cs = preflight.run(spec, offline=offline, clock=RealClock())
    for c in cs:
        print(f"{c.status.upper():5} {c.name:<18} {c.detail}")
    b = preflight.budget_math(spec)
    print(f"budget: max cost if the lease runs to its platform limit = ${b['worst_case_usd']:.2f} (cap ${b['cap_usd_hr']:.3f}/h x {b['limit_h']} h)")
    return not preflight.failed(cs)


def cmd_hardware(a: argparse.Namespace) -> int:
    rows = ddc.public_get("/v1/gpu-prices").get("models") or []
    print(f"{'MODEL':<14}{'VRAM':<8}{'IFACE':<7}{'FREE/TOTAL':<12}{'MIN':>7}{'MED':>7}{'MAX':>7}  ($/h)")
    for m in sorted(rows, key=lambda r: ((r.get("price") or {}).get("med") or 1e9)):
        if a.gpu and a.gpu.lower() not in str(m.get("model", "")).lower():
            continue
        p, av = m.get("price") or {}, m.get("availability") or {}

        def f(v):
            return "-" if v is None else f"{float(v):.2f}"
        print(f"{m.get('model', '?'):<14}{m.get('ram', ''):<8}{m.get('interface', ''):<7}{av.get('available', '?')}/{av.get('total', '?'):<9}{f(p.get('min')):>7}{f(p.get('med')):>7}{f(p.get('max')):>7}")
    print("CPU-only: omit [hardware.gpu] in the spec; there is no public CPU price feed, the SDL cap bounds the price.")
    return 0


def _job_name(v: str) -> str:
    if not NAME_RE.fullmatch(v):
        ddc.die(f"{v!r} is not a job name (lowercase letters, digits, dashes)")
    return v


def _latest_telemetry(job: str | None) -> Path | None:
    name = _job_name(job) if job else "*"
    d = ddc.STATE_DIR / "telemetry"
    files = sorted(d.glob(f"{name}-*.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    return files[-1] if files else None


def cmd_close(a: argparse.Namespace) -> int:
    """Close the active job lease(s) named JOB (or with that dseq).  Only owned role-"job" leases qualify, and the close itself is the
    controller's: ownership assertions, confirmed closed by the platform, absent from spend-rate."""
    want = a.job
    leases = {k: v for k, v in ddc.job_leases().items() if k == want or v.get("job") == want}
    if not leases:
        print(f"no active job lease for {want!r} (see `ddc status`)")
        return 0
    rc = 0
    for dseq in leases:
        try:
            cost = ddc.close_box(dseq)
            print(f"closed {dseq} and verified absent from spend-rate; cost ${cost:.3f}")
        except ddc.DdcError as e:
            print(f"ERROR: dseq {dseq} not closed: {e}", file=sys.stderr)
            rc = 1
    rec_path = ddc.STATE_DIR / "jobs" / f"{want}.json"
    rec = ddc.read_json(rec_path, None)
    if rec and rc == 0:
        rec["status"] = "closed"
        ddc.write_json(rec_path, rec)
    return rc


def main(argv: list[str] | None = None, prog: str = "ddc job") -> int:
    ap = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("run")
    p.add_argument("spec")
    p.add_argument("--keep", action="store_true", help="leave the lease running after SUCCESS (failures always close; the watchdog still closes it after job_orphan_minutes)")
    p.add_argument("--sequential", action="store_true", help="legacy ordering (lease at once, prep after boot, setup after prep): for comparison/debugging")
    p.add_argument("--no-prebuilt", action="store_true", help="skip the prebuilt image, use the stock image")
    p.add_argument("--dry-run", action="store_true", help="preflight + print SDL, create nothing")
    p.add_argument("--offline", action="store_true", help="skip network preflight checks (the spending gate still runs when the lease is created)")
    p.add_argument("--max-cost", type=float, help="override max_cost_usd (hard budget guard); still clamped to budget_usd")
    p = sp.add_parser("check")
    p.add_argument("spec")
    p.add_argument("--offline", action="store_true")
    p = sp.add_parser("sdl")
    p.add_argument("spec")
    p.add_argument("--kind", default="")
    p = sp.add_parser("hardware")
    p.add_argument("--gpu", default="")
    p = sp.add_parser("report")
    p.add_argument("job", nargs="?")
    p = sp.add_parser("close")
    p.add_argument("job")
    a = ap.parse_args(argv)
    try:
        if ddc.CONFIG_ERROR:   # same rule as the `ddc` command: no config (or a broken one) means no run on built-in defaults
            ddc.die(ddc.CONFIG_ERROR)
        if a.cmd == "hardware":
            return cmd_hardware(a)
        if a.cmd == "close":
            return cmd_close(a)
        if a.cmd == "report":
            f = _latest_telemetry(a.job) or ddc.die("no telemetry yet")
            print(format_summary(json.loads(f.read_text(encoding="utf-8"))))
            return 0
        spec = load_spec(a.spec)
        if getattr(a, "max_cost", None):
            spec["max_cost_usd"] = a.max_cost
        if a.cmd == "sdl":
            pub = ddc.pubkey("access") if (ddc.KEYS / "access.pub").exists() else "ssh-ed25519 AAAA-not-generated-yet"
            for note in preflight.apply_limits(spec):
                print(f"# limit: {note}")
            for kind, image in image_kinds(spec):
                if not a.kind or a.kind == kind:
                    print(f"# --- {kind}: {image}\n{render_sdl(spec, image, pub)}")
            return 0
        if a.cmd == "check":
            return 0 if _checks(spec, a.offline) else 2
        if a.dry_run:
            ok = _checks(spec, a.offline)
            print("dry run: nothing created.")
            return 0 if ok else 2
        JobRunner(spec, overlap=not a.sequential, keep=a.keep, prefer_prebuilt=not a.no_prebuilt, offline=a.offline).run()
        return 0
    except (SpecError, JobError, ddc.DdcError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted (lease closed or closing; see summary above)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main(prog="python -m clouddeck.job"))
