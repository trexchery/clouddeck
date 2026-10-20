"""ddc job  (also: python -m clouddeck.job)

  run SPEC      preflight, then bid early / lease late, run the steps, retrieve + verify artifacts, close + verify (needs --approve-usd USD)
  check SPEC    preflight only (add --offline to skip the network checks)
  sdl SPEC      print the SDL that would be deployed
  hardware      public GPU price/availability table (read-only)
  workflows     list the ready-made workflows (Wan 2.2, SCAIL-2, your own ComfyUI graph); --copy DIR makes an editable copy to run them from
  report [JOB]  print the latest telemetry summary
  close JOB     verified close of a job's lease (e.g. after --keep or a killed run)

Every lease is created through the controller's spending gate (budget guard, lease caps, price and runtime ceilings) and closed only
through its ownership-checked close. See docs/JOBS.md.  --json (run, check, hardware, report, close): log lines go to stderr, stdout is one JSON object.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
import re
import shutil
import sys
from pathlib import Path

from clouddeck import controller as ddc

from . import preflight
from .clock import RealClock
from .runner import JobError, JobRunner
from .spec import NAME_RE, SpecError, image_kinds, load_spec, render_sdl
from .telemetry import format_summary


def _checks(spec, offline: bool) -> tuple[int, dict]:
    cs = preflight.run(spec, offline=offline, clock=RealClock())
    for c in cs:
        print(f"{c.status.upper():5} {c.name:<18} {c.detail}")
    b = preflight.budget_math(spec)
    print(f"budget: max cost if the lease runs to its platform limit = ${b['worst_case_usd']:.2f} (cap ${b['cap_usd_hr']:.3f}/h x {b['limit_h']} h)")
    ok = not preflight.failed(cs)
    return (0 if ok else 2), {"ok": ok, "job": spec["name"], "checks": [{"status": c.status, "name": c.name, "detail": c.detail} for c in cs], "worst_case_usd": b["worst_case_usd"],
                              "cap_usd_hr": b["cap_usd_hr"], "limit_h": b["limit_h"], "approve_usd": ddc.approve_hint(b["worst_case_usd"]), "gpu": spec["hardware"]["gpu"], "image": spec["image"]}


def cmd_hardware(a: argparse.Namespace) -> tuple[int, dict]:
    rows = []
    for m in sorted(ddc.public_get("/v1/gpu-prices").get("models") or [], key=lambda r: ((r.get("price") or {}).get("med") or 1e9)):
        p, av = m.get("price") or {}, m.get("availability") or {}
        vram = re.match(r"\d+", str(m.get("ram") or ""))
        free = av.get("available")
        if (a.gpu and a.gpu.lower() not in str(m.get("model", "")).lower()) or (a.min_vram and int(vram.group() if vram else 0) < a.min_vram) or (a.available and not (free or 0) > 0):
            continue
        rows.append({"vendor": m.get("vendor"), "model": m.get("model"), "ram": m.get("ram"), "interface": m.get("interface"), "available": av.get("available"), "total": av.get("total"),
                     "price_min": p.get("min"), "price_med": p.get("med"), "price_max": p.get("max")})
    print(f"{'MODEL':<14}{'VRAM':<8}{'IFACE':<7}{'FREE/TOTAL':<12}{'MIN':>7}{'MED':>7}{'MAX':>7}  ($/h)")
    for r in rows:
        def f(v):
            return "-" if v is None else f"{float(v):.2f}"
        print(f"{r['model'] or '?':<14}{r['ram'] or '':<8}{r['interface'] or '':<7}{'?' if r['available'] is None else r['available']}/{'?' if r['total'] is None else r['total']:<9}"
              f"{f(r['price_min']):>7}{f(r['price_med']):>7}{f(r['price_max']):>7}")
    note = "CPU-only: omit [hardware.gpu] in the spec; there is no public CPU price feed, the SDL cap bounds the price."
    print(note)
    return 0, {"ok": True, "models": rows, "note": note}


def _job_name(v: str) -> str:
    if not NAME_RE.fullmatch(v):
        ddc.die(f"{v!r} is not a job name (lowercase letters, digits, dashes)")
    return v


def _latest_telemetry(job: str | None) -> Path | None:
    name = _job_name(job) if job else "*"
    d = ddc.STATE_DIR / "telemetry"
    files = sorted(d.glob(f"{name}-*.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    return files[-1] if files else None


def _skip_workflow_files(directory: str, names: list[str]) -> list[str]:
    """Same rule as setup.py: a copy of a checkout's workflows/ must not carry that checkout's media, results or my-*.toml specs."""
    return [n for n in names if n in ("__pycache__", "outputs") or n.endswith(".pyc") or (n.startswith("my-") and n.endswith(".toml")) or (Path(directory).name == "inputs" and n != "README.md")]


def cmd_workflows(a: argparse.Namespace) -> tuple[int, dict]:
    src = ddc.data_file("workflows")
    if not src.is_dir():
        raise JobError("this install has no workflows directory")
    specs = []
    for f in sorted(src.glob("*.toml")):
        first = next((ln.lstrip("# ").strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.startswith("#")), "")
        specs.append({"spec": f.name, "about": first})
    out: dict = {"ok": True, "workflows": specs}
    if a.copy:
        dest = Path(a.copy).resolve()
        if dest.exists() and any(dest.iterdir()):
            raise JobError(f"{dest} is not empty; pick a new directory (nothing was copied)")
        shutil.copytree(src, dest, ignore=_skip_workflow_files, dirs_exist_ok=True)
        (dest / "inputs").mkdir(exist_ok=True)
        out["copied_to"] = str(dest)
        print(f"copied {len(specs)} workflows to {dest}\nput your media in {dest / 'inputs'}, then:  ddc job check {dest / specs[0]['spec']}   (read the README.md there first)")
    else:
        for w in specs:
            print(f"  {w['spec']:<24} {w['about']}")
        print("\nddc job workflows --copy DIR   copy them (and their scripts and README) to DIR; run `ddc job check DIR/<spec>` from there")
    return 0, out


def cmd_close(a: argparse.Namespace) -> tuple[int, dict]:
    """Close the active job lease(s) named JOB (or with that dseq).  Only owned role-"job" leases qualify, and the close itself is the
    controller's: ownership assertions, confirmed closed by the platform, absent from spend-rate."""
    want = a.job
    leases = {k: v for k, v in ddc.job_leases().items() if k == want or v.get("job") == want}
    if not leases:
        print(f"no active job lease for {want!r} (see `ddc status`)")
        return 0, {"ok": True, "found": False, "closed": [], "errors": []}
    rc, closed, errors = 0, [], []
    for dseq in leases:
        try:
            cost = ddc.close_box(dseq)
            print(f"closed {dseq} and verified absent from spend-rate; cost ${cost:.3f}")
            closed.append({"dseq": dseq, "cost_usd": cost})
        except ddc.DdcError as e:
            print(f"ERROR: dseq {dseq} not closed: {e}", file=sys.stderr)
            errors.append(f"{dseq}: {e}")
            rc = 1
    rec_path = ddc.STATE_DIR / "jobs" / f"{want}.json"
    rec = ddc.read_json(rec_path, None)
    if rec and rc == 0:
        rec["status"] = "closed"
        ddc.write_json(rec_path, rec)
    return rc, {"ok": rc == 0, "found": True, "closed": closed, "errors": errors}


def cmd_report(a: argparse.Namespace) -> tuple[int, dict]:
    f = _latest_telemetry(a.job) or ddc.die("no telemetry yet")
    t = json.loads(f.read_text(encoding="utf-8"))
    print(format_summary(t))
    return 0, {"ok": True, "telemetry_file": str(f), "telemetry": t}


def cmd_run(a: argparse.Namespace, spec: dict) -> tuple[int, dict]:
    lim = copy.deepcopy(spec)   # the runner applies (and logs) the limits itself; this copy only prices the worst case it will run with
    preflight.apply_limits(lim)
    ddc.require_approval(a.approve_usd, preflight.budget_math(lim)["worst_case_usd"], f"ddc job run {spec['name']}")
    r = JobRunner(spec, overlap=not a.sequential, keep=a.keep, prefer_prebuilt=not a.no_prebuilt, offline=a.offline)
    err: BaseException | None = None
    try:
        r.run()
        if r.dseq and not r.closed and not a.keep:   # the runner logs a failed close and returns: the CLI must not report success while the lease may bill
            err = JobError(f"lease dseq {r.dseq} was NOT verified closed and may still bill: run `ddc job close {spec['name']}` / `ddc status`")
    except (Exception, KeyboardInterrupt) as e:   # RemoteLost, HostKeyChanged, OSError ...: the JSON must still say whether the lease closed
        err = e
    if err is not None and not a.json:
        raise err
    out = {"ok": err is None, "job": spec["name"], "dseq": r.dseq, "provider": r.provider, "usd_hr": r.usd_hr, "cost_usd": r.cost, "closed": r.closed,
           "artifacts": r.artifacts, "artifacts_dir": str(r.art_dir.resolve()), "telemetry_file": str(r.tel.path), "summary": r.tel.to_dict(r.usd_hr)["summary"],
           "error": None if err is None else (str(err) or type(err).__name__)} | ({"kept": True} if a.keep and err is None and r.dseq and not r.closed else {})
    return (0 if err is None else 130 if isinstance(err, KeyboardInterrupt) else 1), out


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
    p.add_argument("--approve-usd", type=float, metavar="USD", help="the spend the owner approved for this run (required unless --dry-run; run without it to see the worst case)")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    p = sp.add_parser("check")
    p.add_argument("spec")
    p.add_argument("--offline", action="store_true")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    p = sp.add_parser("sdl")
    p.add_argument("spec")
    p.add_argument("--kind", default="")
    p = sp.add_parser("hardware")
    p.add_argument("--gpu", default="")
    p.add_argument("--min-vram", type=int, default=0, metavar="GI", help="only models with at least this much VRAM")
    p.add_argument("--available", action="store_true", help="only models with free units")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    p = sp.add_parser("workflows")
    p.add_argument("--copy", metavar="DIR", help="copy the workflows (specs, scripts, README, an empty inputs/) into a new or empty directory")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    p = sp.add_parser("report")
    p.add_argument("job", nargs="?")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    p = sp.add_parser("close")
    p.add_argument("job")
    p.add_argument("--json", action="store_true", help="log lines to stderr, one JSON object on stdout")
    a = ap.parse_args(argv)
    ddc.APPROVAL = None   # an approval never outlives the command it was given to
    js = getattr(a, "json", False)
    try:
        with contextlib.redirect_stdout(sys.stderr) if js else contextlib.nullcontext():
            rc, out = _dispatch(a)
    except (SpecError, JobError, ddc.DdcError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        rc, out = 1, {"ok": False, "error": str(e)}
    except KeyboardInterrupt:
        print("interrupted (lease closed or closing; see summary above)", file=sys.stderr)
        rc, out = 130, {"ok": False, "error": "interrupted"}
    except Exception as e:
        if not js:
            raise
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        rc, out = 1, {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if js:
        print(json.dumps(out, default=str))
    return rc


def _dispatch(a: argparse.Namespace) -> tuple[int, dict]:
    if ddc.CONFIG_ERROR and a.cmd not in ("hardware", "workflows"):   # same rule as the `ddc` command: no config (or a broken one) means no run on built-in defaults; the public price table needs none
        ddc.die(ddc.CONFIG_ERROR)
    if a.cmd == "hardware":
        return cmd_hardware(a)
    if a.cmd == "workflows":
        return cmd_workflows(a)
    if a.cmd == "close":
        return cmd_close(a)
    if a.cmd == "report":
        return cmd_report(a)
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
        return 0, {}
    if a.cmd == "check":
        return _checks(spec, a.offline)
    if a.dry_run:
        rc, out = _checks(spec, a.offline)
        print("dry run: nothing created.")
        return rc, out
    return cmd_run(a, spec)


if __name__ == "__main__":
    raise SystemExit(main(prog="python -m clouddeck.job"))
