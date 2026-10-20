"""Checks that run before any money is spent. `fail` blocks the run, `warn` is reported, `skip` = needs network (offline mode).

Network checks only read public endpoints and the account balance; they never create or change anything.  The spending part is not
re-implemented here: it calls the controller's `check_spend` (the gate every lease passes at creation, see `ddc.launch_lease`), so what
preflight predicts is what the launch will enforce.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clouddeck import controller as ddc
from clouddeck import sdlgen

from .spec import SECRET_NAME, SHA256_RE, SpecError, image_kinds, render_sdl, size_gi


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


def failed(checks: list[Check]) -> bool:
    return any(c.status == "fail" for c in checks)


def apply_limits(s: dict[str, Any]) -> list[str]:
    """Clamp the spec's own caps to the controller's configuration (never above it) and say what changed.  The price cap per lease may
    not exceed `price_ceiling()` (`max_price_usd_hr`), the platform runtime limit not the hard runtime ceiling, and `max_cost_usd` not
    the configured budget while the budget guard is on.  `unsafe_disable_limits = true` lifts the first two, as everywhere else."""
    notes: list[str] = []
    hw = s["hardware"]
    if not ddc.limits_unsafe():
        if hw["max_usd_hr"] and hw["max_usd_hr"] > ddc.price_ceiling():
            notes.append(f"hardware.max_usd_hr ${hw['max_usd_hr']:g}/h lowered to the configured ceiling ${ddc.price_ceiling():g}/h (`max_price_usd_hr`)")
            hw["max_usd_hr"] = ddc.price_ceiling()
        if s["runtime_hours"] > ddc.HARD_MAX_RUNTIME_H:
            notes.append(f"runtime_hours {s['runtime_hours']:g} lowered to the hard ceiling {ddc.HARD_MAX_RUNTIME_H} h")
            s["runtime_hours"] = float(ddc.HARD_MAX_RUNTIME_H)
    budget = ddc.CFG.get("budget_usd")
    if ddc.CFG.get("budget_guard", True) and ddc._num(budget) and s["max_cost_usd"] > budget:
        notes.append(f"max_cost_usd ${s['max_cost_usd']:g} lowered to the configured budget_usd ${budget:g}")
        s["max_cost_usd"] = float(budget)
    return notes


def limit_h(s: dict[str, Any]) -> int:
    """runtimeLimitHours of the lease: the runtime rounded up to whole hours (always at least 1)."""
    return max(1, int(s["runtime_hours"] + 0.999))


def budget_math(s: dict[str, Any]) -> dict[str, float]:
    cap = float(s["hardware"]["max_usd_hr"] or 0)
    worst = cap * limit_h(s)   # what the platform limit can cost at the price cap (the lease is charged up to runtimeLimitHours)
    return {"cap_usd_hr": cap, "runtime_hours": s["runtime_hours"], "limit_h": limit_h(s), "worst_case_usd": round(worst, 4), "max_cost_usd": s["max_cost_usd"]}


def spend_label(s: dict[str, Any]) -> str:
    return f"job {s['name']} lease"


def run(s: dict[str, Any], offline: bool = False, clock=None) -> list[Check]:
    out: list[Check] = []

    def add(n: str, st: str, d: str) -> None:
        out.append(Check(n, st, d))
    hw, base = s["hardware"], Path(s["base"])
    for note in apply_limits(s):
        add("limits", "warn", note)

    # SDL validation (with a placeholder key if the real one is not generated yet)
    key_ok = (ddc.KEYS / "access.pub").exists()
    add("ssh_key", "ok" if key_ok else "fail", str(ddc.KEYS / "access.pub") if key_ok else f"missing {ddc.KEYS / 'access.pub'} (run `ddc setup`, or ssh-keygen -t ed25519 -N '' -f {ddc.KEYS / 'access'})")
    try:
        pub = ddc.pubkey("access") if key_ok else "ssh-ed25519 AAAAPREFLIGHT"
        for kind, image in image_kinds(s):
            sdl = render_sdl(s, image, pub)
            add(f"sdl[{kind}]", "ok" if len(sdl) < 64_000 else "warn", f"valid, {len(sdl)} bytes")
    except SpecError as e:
        add("sdl", "fail", str(e).replace("\n", " "))
    for kind, image in image_kinds(s):
        if "@sha256:" not in image:   # the controller's own wording for its image (config_warnings)
            add(f"image_tag[{kind}]", "warn", f"{image} is a mutable tag, not pinned by @sha256:<digest>: the box runs whatever the tag points to when it starts "
                "(providers also cache layers by tag, so a moving tag defeats the image cache hit)")

    # hardware bounds of the controller's config (max_cpu, max_mem_gb, max_disk_gb, max_gpu)
    lim = sdlgen.bounds(ddc.CFG)
    over = [f"{k} {v}" for k, v, cap in (("cpu", hw["cpu"], lim["max_cpu"]), ("memory", size_gi(hw["memory"]), lim["max_mem_gb"]), ("disk", size_gi(hw["disk"]), lim["max_disk_gb"]),
                                         ("gpu", (hw["gpu"] or {}).get("units", 0), lim["max_gpu"])) if v > cap]
    if over:
        add("hardware_limits", "fail", f"above the limits of config.toml (max_cpu/max_mem_gb/max_disk_gb/max_gpu): {', '.join(over)}")

    # local inputs (those produced by `prep` appear after it runs)
    prod = set(s["prep"]["produces"])
    miss = [p for p in s["inputs"] if p not in prod and not (base / p).is_file()]
    empty = [p for p in s["inputs"] if (base / p).is_file() and (base / p).stat().st_size == 0]
    total = sum((base / p).stat().st_size for p in s["inputs"] if (base / p).is_file())
    add("inputs", "fail" if miss or empty else "ok",
        (f"missing {miss}" if miss else "") + (f" empty {empty}" if empty else "") or f"{len(s['inputs'])} file(s), {total / 1e9:.2f} GB local; {len(prod)} more produced by prep")
    if prod and not s["prep"]["cmd"]:
        add("prep", "fail", "prep.produces is set but prep.cmd is empty")

    # disk / memory sanity
    need_gb = (sum(i["size_gb"] for i in s["cache"]["items"]) + total / 1e9 + s["scratch_gb"]) * 1.1
    disk = size_gi(hw["disk"])
    add("disk", "fail" if disk < need_gb else "ok", f"{disk:.0f} GiB requested, ~{need_gb:.0f} GB needed (cache + inputs + {s['scratch_gb']:.0f} GB scratch, +10%)")
    mem = size_gi(hw["memory"])
    add("memory", "fail" if mem < s["min_memory_gi"] else "ok", f"{mem:.0f} GiB requested" + (f", job needs >= {s['min_memory_gi']:.0f}" if s["min_memory_gi"] else ""))
    g = hw["gpu"]
    if g and g["min_vram_gi"]:
        ok = [m for m in g["models"] if m["ram"] and size_gi(m["ram"]) >= g["min_vram_gi"]]
        add("vram", "ok" if ok and len(ok) == len(g["models"]) else "fail" if not ok else "warn",
            f"models {[m['model'] + ':' + m['ram'] for m in g['models']]} vs min {g['min_vram_gi']:.0f} GiB")

    # cache integrity metadata and secrets
    nosha = [i["name"] for i in s["cache"]["items"] if not i["sha256"]]
    badsha = [i["name"] for i in s["cache"]["items"] if i["sha256"] and not SHA256_RE.fullmatch(i["sha256"])]
    if badsha:
        add("cache", "fail", f"malformed sha256 for {badsha}")
    elif nosha:
        add("cache", "warn", f"allow_unverified is set for {nosha}: those downloads are transport-checked only, a bad mirror would go undetected")
    elif s["cache"]["items"]:
        add("cache", "ok", f"{len(s['cache']['items'])} item(s) with sha256, {len(s['cache']['mirrors'])} mirror(s) before origin")
    unset = [k for k in s["secrets"] if k not in os.environ]
    add("secrets", "fail" if unset else "ok",
        f"not set in the local environment: {unset}" if unset else
        (f"{len(s['secrets'])} variable(s) of your local environment will be pushed over SSH to the rented host: {', '.join(s['secrets'])} (names only, values never printed; "
         "not in the SDL; the host can read them while the lease runs)") if s["secrets"] else "no secrets are pushed")
    leak = [k for k in s["env"] if SECRET_NAME.search(k)]
    if leak:
        add("env", "fail", f"secret-looking env in SDL: {leak}")

    # spec-level budget math: the spec's own cap (already clamped to the config above)
    b = budget_math(s)
    over_cap = b["worst_case_usd"] > b["max_cost_usd"]
    add("budget", "fail" if over_cap else "ok",
        f"worst case = {b['limit_h']} h platform limit x ${b['cap_usd_hr']:.3f}/h cap = ${b['worst_case_usd']:.2f} "
        f"{'EXCEEDS' if over_cap else 'within'} max_cost_usd ${b['max_cost_usd']:.2f}" + (" (lower runtime_hours or the price cap, or raise max_cost_usd deliberately)" if over_cap else ""))

    if offline:
        for n in ("api_key", "spend_gate", "price_vs_cap", "provider_health"):
            add(n, "skip", "offline (the spending gate is enforced again when the lease is created)")
        return out

    secrets = ddc.load_secrets()
    add("api_key", "ok" if secrets.get("AKASH_API_KEY") else "fail", "AKASH_API_KEY present (value not read into output)" if secrets.get("AKASH_API_KEY") else f"AKASH_API_KEY missing in {ddc.SECRETS_DIR / 'secrets.env'}")
    try:
        out += _market_checks(s, b, clock)
    except Exception as e:  # noqa: BLE001  (API unreachable must block: we would be flying blind)
        add("market", "fail", f"Akash API not usable: {e}")
    return out


def _market_checks(s: dict[str, Any], b: dict[str, float], clock) -> list[Check]:
    out: list[Check] = []

    def add(n: str, st: str, d: str) -> None:
        out.append(Check(n, st, d))
    hw, g = s["hardware"], s["hardware"]["gpu"]
    provs = [p for p in ddc.public_get("/v1/providers")]
    ok = [p for p in provs if p.get("isOnline") and float(p.get("uptime7d") or 0) >= hw["min_uptime"]]
    perf = ddc.read_json(ddc.PERF, {})
    t = clock.time() if clock else 0
    ok = [p for p in ok if float((perf.get(p["owner"]) or {}).get("bad_until") or 0) <= t]
    if g:
        want = {m["model"] for m in g["models"]}
        ok = [p for p in ok if any(m.get("model") in want for m in (p.get("gpuModels") or []))]
        free = sum(int((p.get("stats") or {}).get("gpu", {}).get("available") or 0) for p in ok)
        add("provider_health", "ok" if ok and free >= g["units"] else "fail",
            f"{len(ok)} online provider(s) with uptime7d >= {hw['min_uptime']} offering {sorted(want)}; {free} GPU(s) free in total")
        rows = [m for m in (ddc.public_get("/v1/gpu-prices").get("models") or []) if m.get("model") in want
                and any(not x["ram"] or x["ram"] == m.get("ram") for x in g["models"] if x["model"] == m.get("model"))]
        lows = [float((m.get("price") or {}).get("min")) for m in rows if (m.get("price") or {}).get("min") is not None]
        if not rows:
            add("price_vs_cap", "warn", "no matching row in /v1/gpu-prices")
        elif not lows:
            add("price_vs_cap", "warn", "gpu-prices has no price data for these models")
        else:
            cap, lo = b["cap_usd_hr"], min(lows)
            avail = sum(int((m.get("availability") or {}).get("available") or 0) for m in rows)
            add("price_vs_cap", "fail" if lo > cap else "ok" if cap >= lo * 1.15 else "warn",
                f"cap ${cap:.2f}/h vs lowest observed ${lo:.2f}/h ({avail} unit(s) available)" + ("; cap is below every price, no bid can win" if lo > cap else ""))
    else:
        add("provider_health", "ok" if ok else "fail", f"{len(ok)} online provider(s) with uptime7d >= {hw['min_uptime']}")
        add("price_vs_cap", "skip", "no public CPU price feed; the cap is enforced by the SDL price (no bid above it can win)")
    # the controller's own gate (config, lease caps incl. pending creations, ceilings, balance, budget/reserve): read-only
    try:
        ddc.check_spend(spend_label(s), 1, b["limit_h"], b["cap_usd_hr"], "job")
        add("spend_gate", "ok", f"check_spend passes for 1 job lease x {b['limit_h']} h at ${b['cap_usd_hr']:.3f}/h (see the budget line above in the log)")
    except ddc.DdcError as e:
        add("spend_gate", "fail", f"the controller would refuse this lease: {e}")
    return out
