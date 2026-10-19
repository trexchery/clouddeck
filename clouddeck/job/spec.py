"""Job spec (TOML) -> normalized dict, and SDL generation/validation for any CPU/GPU Akash workload.

Nothing here is hardware specific: GPU vendor/models/VRAM, CPU, memory and disk all come from the spec.  GPU ids are shaped by
`clouddeck.sdlgen` (the same token / VRAM rules as the profiles of the controller), so a model that appears on the network tomorrow works
without a code change.
"""
from __future__ import annotations

import base64
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from clouddeck import controller as ddc
from clouddeck import sdlgen

BASE_APT = ["openssh-server", "rsync", "curl", "ca-certificates", "aria2", "tmux"]
SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I)
# Every one of these is used with `fullmatch` (`^...$` + `match` would also accept a trailing newline).
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")
STEP_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)")
GLOB_RE = re.compile(r"[A-Za-z0-9._*?/\[\]-]+")    # artifact / produced-file patterns: expanded by the remote shell, so only plain glob characters
SHA256_RE = re.compile(r"[0-9a-f]{64}")
URL_RE = re.compile(r"https?://[^\s\x00-\x1f\x7f]+")   # download sources: http(s) only, never something curl/aria2c could read as an option
RESERVED_SECRET_PREFIXES = ("AKASH_", "DDC_")      # controller / Akash credentials and ddc's own variables: never pushed to a rented host
_WIN_DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])", re.I)
DEFAULT_MAX_ARTIFACT_GB = 20.0   # conservative default of `max_artifact_gb`
SERVICE = "job"


def boot_script() -> Path:
    return ddc.data_file("jobremote") / "boot.sh"


class SpecError(ValueError):
    pass


def size_gi(s: str) -> float:
    """'96Gi' / '512Mi' / '1Ti' -> GiB (decimal K/M/G/T treated as the binary unit, close enough for sanity checks)."""
    m = SIZE_RE.fullmatch(str(s))
    if not m:
        raise SpecError(f"bad size {s!r} (use e.g. 512Mi, 96Gi, 1Ti)")
    n, u = float(m.group(1)), m.group(2)[0]
    return n * {"K": 1 / 1024 ** 2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}[u]


_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")   # everything but \t and \n: escape sequences (ESC, the C1 CSI), carriage returns, bells ...


def scrub(text: Any, limit: int = 4000) -> str:
    """Text that came from the box (log tails, reasons, names), made safe to print: control and escape characters become `?`, so a hostile
    box cannot move the cursor, recolour the terminal or forge a log line."""
    return _CONTROL.sub("?", str(text))[-limit:]


def safe_rel_path(p: str, glob: bool = False) -> bool:
    """A remote-reported or spec-given path that stays under its base directory: relative, no `..`, no empty component, no control character,
    and nothing Windows would treat specially (backslash, `:` i.e. drives and alternate data streams, `<>"|`, reserved device names such as CON or
    NUL, a trailing dot or space).  `glob=True` also allows the glob characters `* ? [ ]`."""
    p = str(p)
    if not p or p.startswith("/") or "\\" in p or any(ord(c) < 32 or c == "\x7f" for c in p):
        return False
    if any(c in (':<>"|' if glob else ':<>"|?*') for c in p):
        return False
    for x in p.split("/"):
        if x in ("", "..") or x[-1] in ". ":
            return False
        if _WIN_DEVICE.fullmatch(x.split(".")[0].strip()):
            return False
    return True


def load_spec(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return normalize(tomllib.loads(p.read_text(encoding="utf-8")), p.resolve().parent)


def _num(raw: dict[str, Any], key: str, default: float, err: list[str], where: str = "") -> float:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        err.append(f"{where}{key} must be a number, got {v!r}")
        return float(default)
    return float(v)


def normalize(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    err: list[str] = []
    s: dict[str, Any] = {"base": str(base)}
    s["name"] = str(raw.get("name", ""))
    if not NAME_RE.fullmatch(s["name"]):
        err.append("name: required, lowercase letters/digits/dashes, max 31 chars")
    s["runtime_hours"] = _num(raw, "runtime_hours", 4, err)
    s["max_cost_usd"] = _num(raw, "max_cost_usd", 10.0, err)
    s["estimated_runtime_min"] = _num(raw, "estimated_runtime_min", 60, err)
    s["artifacts_dir"] = str(raw.get("artifacts_dir", "outputs"))
    if not safe_rel_path(s["artifacts_dir"]):
        err.append("artifacts_dir: a relative path below the spec's directory (no .. and no leading /)")
    s["poll_s"] = _num(raw, "poll_s", 10, err)
    s["idle_abort_s"] = _num(raw, "idle_abort_s", 900, err)
    s["max_prep_wait_s"] = _num(raw, "max_prep_wait_s", 3600, err)
    s["bid_wait_s"] = _num(raw, "bid_wait_s", 20, err)
    s["setup_estimate_s"] = _num(raw, "setup_estimate_s", 0, err)
    s["cold_boot_s"] = _num(raw, "cold_boot_s", 180, err)  # expected lease->sshd on a provider that has not run this image before
    if "deposit_usd" in raw:
        err.append("deposit_usd is not supported: escrow deposits are decided by the controller, not by a job spec")
    s["chunk_mb"] = int(_num(raw, "chunk_mb", 64, err))
    s["salvage_s"] = _num(raw, "salvage_s", 120, err)
    s["min_memory_gi"] = _num(raw, "min_memory_gi", 0, err)
    s["scratch_gb"] = _num(raw, "scratch_gb", 10, err)
    s["max_artifact_gb"] = _num(raw, "max_artifact_gb", DEFAULT_MAX_ARTIFACT_GB, err)   # cap on what the runner pulls back (per file and per job): the box reports the sizes
    if not 0 < s["max_artifact_gb"] <= 100000:
        err.append("max_artifact_gb must be > 0")
    if not 0 < s["runtime_hours"] < 1e4:
        err.append("runtime_hours must be > 0")
    if s["max_cost_usd"] <= 0:
        err.append("max_cost_usd must be > 0")
    if s["chunk_mb"] < 1 or s["poll_s"] <= 0:
        err.append("chunk_mb must be >= 1 and poll_s > 0")

    h = dict(raw.get("hardware") or {})
    hw = {"cpu": _num(h, "cpu", 4, err, "hardware."), "memory": str(h.get("memory", "8Gi")), "disk": str(h.get("disk", "20Gi")),
          "max_usd_hr": h.get("max_usd_hr"), "min_uptime": _num(h, "min_uptime", 0.95, err, "hardware."), "gpu": None}
    if not 0 < hw["cpu"] <= sdlgen.ABS_MAX["max_cpu"]:
        err.append(f"hardware.cpu must be > 0 and <= {sdlgen.ABS_MAX['max_cpu']}")
    if hw["max_usd_hr"] is None:
        err.append("hardware.max_usd_hr: required (it becomes the SDL price cap, so no bid above it can win)")
    elif isinstance(hw["max_usd_hr"], bool) or not isinstance(hw["max_usd_hr"], (int, float)) or hw["max_usd_hr"] <= 0:
        err.append(f"hardware.max_usd_hr must be a positive number, got {hw['max_usd_hr']!r}")
        hw["max_usd_hr"] = None
    else:
        hw["max_usd_hr"] = float(hw["max_usd_hr"])
    for k in ("memory", "disk"):
        try:
            size_gi(hw[k])
        except SpecError as e:
            err.append(f"hardware.{k}: {e}")
    g = h.get("gpu")
    if g:
        models = []
        for m in g.get("models") or []:
            name, _, vram = str(m).partition(":")
            model = sdlgen.norm_model(name)
            if not sdlgen.TOKEN_RE.fullmatch(model):
                err.append(f"hardware.gpu.models: {str(m)[:40]!r} is not a GPU model id (letters, digits . _ -)")
                continue
            if vram:
                try:
                    vram = sdlgen.ram(vram, f"hardware.gpu.models {str(m)[:40]!r} VRAM")
                except sdlgen.SdlError as e:
                    err.append(str(e))
            if model not in [x["model"] for x in models]:
                models.append({"model": model, "ram": vram})
        if not models:
            err.append("hardware.gpu.models: list at least one, e.g. [\"model-a:80Gi\", \"model-b\"] (VRAM optional; ids as shown by `ddc job hardware`)")
        if len(models) > sdlgen.MAX_ANY_OF:
            err.append(f"hardware.gpu.models: at most {sdlgen.MAX_ANY_OF} models in one order")
        vendor = ""
        if "vendor" not in g:
            err.append("hardware.gpu.vendor: required, the GPU vendor id as the network lists it for the models you chose (no vendor is built in)")
        else:
            try:
                vendor = sdlgen.tok(g["vendor"], "hardware.gpu.vendor")
            except sdlgen.SdlError as e:
                err.append(str(e))
        units = g.get("units", 1)
        if not (isinstance(units, int) and not isinstance(units, bool) and 1 <= units <= sdlgen.ABS_MAX["max_gpu"]):
            err.append(f"hardware.gpu.units must be a whole number from 1 to {sdlgen.ABS_MAX['max_gpu']}")
            units = 1
        hw["gpu"] = {"units": units, "vendor": vendor, "models": models, "min_vram_gi": _num(g, "min_vram_gi", 0, err, "hardware.gpu.")}
    s["hardware"] = hw

    im = dict(raw.get("image") or {})
    s["image"] = {"prebuilt": im.get("prebuilt") or "", "stock": im.get("stock") or "",
                  "prebuilt_timeout_s": _num(im, "prebuilt_timeout_s", 300, err, "image."),
                  "stock_timeout_s": _num(im, "stock_timeout_s", 900, err, "image."),
                  "apt": [str(x) for x in im.get("apt") or []]}
    if not (s["image"]["prebuilt"] or s["image"]["stock"]):
        err.append("image: set image.prebuilt and/or image.stock")
    for k in ("prebuilt", "stock"):
        if s["image"][k] and not (isinstance(s["image"][k], str) and ddc.IMAGE_RE.fullmatch(s["image"][k])):
            err.append(f"image.{k} must be a plain container image reference (letters, digits and . _ / : @ -)")
    for a in s["image"]["apt"]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]{1,60}", a):
            err.append(f"image.apt entry {a[:40]!r} is not a Debian package name")

    e = dict(raw.get("env") or {})
    s["secrets"] = [str(x) for x in e.pop("secrets", [])]
    s["env"] = {str(k): str(v) for k, v in e.items()}
    for k in s["secrets"]:
        if not ENV_NAME.fullmatch(k):
            err.append(f"env.secrets: {k[:40]!r} is not a valid environment variable name")
        elif k.upper().startswith(RESERVED_SECRET_PREFIXES):
            err.append(f"env.secrets: {k} is a controller or Akash credential name (AKASH_* / DDC_*): it is never pushed to a rented host")
    for k in s["env"]:
        if not ENV_NAME.fullmatch(k) or k.startswith("DDC_"):
            err.append(f"env.{k[:40]}: not a valid variable name (DDC_* is reserved)")
        elif SECRET_NAME.search(k):
            err.append(f"env.{k}: looks like a secret and the SDL is visible to the provider; list it in env.secrets instead (pushed over SSH)")

    pr = dict(raw.get("prep") or {})
    s["prep"] = {"cmd": pr.get("cmd") or "", "estimate_s": _num(pr, "estimate_s", 0, err, "prep."), "produces": [str(x) for x in pr.get("produces") or []]}
    s["inputs"] = [str(x) for x in raw.get("inputs") or []]
    s["inputs_dest"] = str(raw.get("inputs_dest", "in")).strip("/")
    if not safe_rel_path(s["inputs_dest"]):
        err.append("inputs_dest: a relative directory below /work (no ..)")
    for p in s["inputs"]:
        if not safe_rel_path(p.replace("\\", "/")):
            err.append(f"inputs: {p[:60]!r} must be a path below the spec's directory (no .. and no leading /)")

    c = dict(raw.get("cache") or {})
    items = []
    for it in c.get("item") or []:
        n = str(it.get("name", ""))
        if not STEP_RE.fullmatch(n):
            err.append(f"cache item name {n!r} invalid")
        urls = [str(u) for u in it.get("urls") or ([it["url"]] if it.get("url") else [])]
        if not it.get("dest") or not urls:
            err.append(f"cache item {n!r}: needs dest and url/urls")
        dest = str(it.get("dest", "")).lstrip("/")
        if dest and not safe_rel_path(dest):
            err.append(f"cache item {n!r}: dest must stay below /work (no ..)")
        sha = str(it.get("sha256", "")).lower()
        unverified = it.get("allow_unverified", False)
        if not isinstance(unverified, bool):
            err.append(f"cache item {n!r}: allow_unverified must be true or false")
            unverified = False
        if not sha and not unverified:
            err.append(f"cache item {n!r}: sha256 is required (a download nobody verifies could be anything); to accept that on purpose set allow_unverified = true")
        elif sha and not SHA256_RE.fullmatch(sha):
            err.append(f"cache item {n!r}: sha256 must be 64 hex characters")
        for u in urls:
            if not URL_RE.fullmatch(u):
                err.append(f"cache item {n!r}: url {u[:60]!r} must be a plain http:// or https:// URL")
        items.append({"name": n, "dest": dest, "urls": urls, "sha256": sha, "allow_unverified": unverified,
                      "size_gb": _num(it, "size_gb", 0, err, f"cache item {n!r}: "), "path": str(it.get("path") or Path(str(it.get("dest", ""))).name)})
    s["cache"] = {"mirrors": [str(m) for m in c.get("mirrors") or []], "items": items}
    for m in s["cache"]["mirrors"]:
        if not URL_RE.fullmatch(m):
            err.append(f"cache.mirrors: {m[:60]!r} must be a plain http:// or https:// URL")

    steps, seen = [], {"inputs", "toolchain"} | {f"cache-{i['name']}" for i in items}
    prev = None
    for st in raw.get("step") or []:
        n = str(st.get("name", ""))
        if not STEP_RE.fullmatch(n) or n in seen:
            err.append(f"step name {n!r} invalid or duplicate")
        seen.add(n)
        if not st.get("run"):
            err.append(f"step {n!r}: run is required")
        if "needs" in st:
            needs = [str(x) for x in st["needs"]]
        else:  # default: sequential, after inputs and every cache item
            needs = (["inputs"] if s["inputs"] or s["prep"]["produces"] else []) + [f"cache-{i['name']}" for i in items] + ([prev] if prev else [])
        arts = [str(a).lstrip("/") for a in st.get("artifacts") or []]
        for a in arts:
            if not (GLOB_RE.fullmatch(a) and safe_rel_path(a, glob=True)):
                err.append(f"step {n!r}: artifact pattern {a[:60]!r} must be a plain relative glob below /work (letters, digits . _ - / * ? [ ], no ..)")
        steps.append({"name": n, "run": str(st.get("run", "")), "needs": needs, "productive": bool(st.get("productive", True)), "artifacts": arts})
        prev = n
    if not steps:
        err.append("at least one [[step]] is required")
    known = seen
    for st in steps:
        for need in st["needs"]:
            if need not in known:
                err.append(f"step {st['name']!r}: unknown need {need!r}")
    s["steps"] = steps
    if err:
        raise SpecError("invalid job spec:\n  " + "\n  ".join(err))
    return s


def image_kinds(s: dict[str, Any], prefer_prebuilt: bool = True) -> list[tuple[str, str]]:
    """Ordered (kind, image) attempts: prebuilt first, stock as the fallback."""
    out = []
    if prefer_prebuilt and s["image"]["prebuilt"]:
        out.append(("prebuilt", s["image"]["prebuilt"]))
    if s["image"]["stock"]:
        out.append(("stock", s["image"]["stock"]))
    return out or [("prebuilt", s["image"]["prebuilt"])]


# ---------------------------------------------------------------- SDL
def uact_block(usd_hr: float) -> int:
    """The SDL price cap: the controller's own conversion (a whole number of uact per block, at least 1)."""
    return max(1, ddc.uact_block_from_usd_hr(usd_hr))


def build_sdl(s: dict[str, Any], image: str, pubkey: str, ports: list[int] | None = None) -> dict[str, Any]:
    hw = s["hardware"]
    boot = boot_script().read_text(encoding="utf-8")
    pkgs = list(dict.fromkeys(BASE_APT + s["image"]["apt"]))
    env = [f"DDC_AUTHORIZED_KEYS_B64={base64.b64encode((pubkey.strip() + chr(10)).encode()).decode()}",
           f"DDC_APT_PKGS={' '.join(pkgs)}", f"DDC_JOB={s['name']}"] + [f"{k}={v}" for k, v in s["env"].items()]
    res: dict[str, Any] = {"cpu": {"units": hw["cpu"] if hw["cpu"] != int(hw["cpu"]) else int(hw["cpu"])},
                           "memory": {"size": hw["memory"]}, "storage": [{"size": hw["disk"]}]}
    if hw["gpu"]:
        g = hw["gpu"]
        res["gpu"] = {"units": g["units"], "attributes": {"vendor": {g["vendor"]: [
            {"model": m["model"], **({"ram": m["ram"]} if m["ram"] else {})} for m in g["models"]]}}}
    expose = [{"port": 22, "as": 22, "to": [{"global": True}]}] + [{"port": p, "as": p, "to": [{"global": True}]} for p in ports or []]
    return {"version": "2.0",
            "services": {SERVICE: {"image": image, "env": env, "command": ["bash", "-c"], "args": [boot], "expose": expose}},
            "profiles": {"compute": {SERVICE: {"resources": res}},
                         "placement": {"akash": {"pricing": {SERVICE: {"denom": "uact", "amount": uact_block(hw["max_usd_hr"] or 0)}}}}},
            "deployment": {SERVICE: {"akash": {"profile": SERVICE, "count": 1}}}}


def validate_sdl(d: dict[str, Any]) -> list[str]:
    e: list[str] = []
    if d.get("version") != "2.0":
        e.append("version must be '2.0'")
    svcs = d.get("services") or {}
    comp = (d.get("profiles") or {}).get("compute") or {}
    price = (((d.get("profiles") or {}).get("placement") or {}).get("akash") or {}).get("pricing") or {}
    dep = d.get("deployment") or {}
    if not svcs:
        e.append("no services")
    for n, sv in svcs.items():
        if not sv.get("image"):
            e.append(f"{n}: image missing")
        for ev in sv.get("env") or []:
            k = ev.split("=", 1)[0]
            if SECRET_NAME.search(k) and k != "DDC_AUTHORIZED_KEYS_B64":
                e.append(f"{n}: env {k} looks like a secret")
        if re.search(r"__[A-Z0-9_]+__", json.dumps(sv)):
            e.append(f"{n}: unrendered placeholder")
        for x in sv.get("expose") or []:
            if not isinstance(x.get("port"), int):
                e.append(f"{n}: expose port must be an integer")
        res = (comp.get(n) or {}).get("resources")
        if not res:
            e.append(f"{n}: no compute profile")
            continue
        if float(res["cpu"]["units"]) <= 0:
            e.append(f"{n}: cpu units must be > 0")
        for k, v in (("memory", res["memory"]["size"]), *[("storage", x["size"]) for x in res["storage"]]):
            if not SIZE_RE.fullmatch(str(v)):
                e.append(f"{n}: bad {k} size {v!r}")
        g = res.get("gpu")
        if g and (int(g["units"]) < 1 or not any(g["attributes"]["vendor"].values())):
            e.append(f"{n}: gpu needs units >= 1 and at least one model")
        p = price.get(n) or {}
        if p.get("denom") != "uact" or int(p.get("amount") or 0) <= 0:
            e.append(f"{n}: placement.akash.pricing needs denom uact and amount > 0")
        pn = ((dep.get(n) or {}).get("akash") or {}).get("profile")
        if pn not in comp:
            e.append(f"{n}: deployment profile {pn!r} not found")
    return e


def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v))  # JSON double-quoted strings are valid YAML


def emit_yaml(o: Any, ind: int = 0) -> list[str]:
    """Tiny YAML emitter (dict/list/str/int/bool) so SDL rendering needs no dependency."""
    pad = "  " * ind
    out: list[str] = []
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out += emit_yaml(v, ind + 1)
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}{k}: |")
                out += [f"{pad}  {ln}" if ln else "" for ln in v.rstrip("\n").split("\n")]
            else:
                out.append(f"{pad}{k}: {_scalar(v)}")
    else:
        for v in o:
            if isinstance(v, dict) and v:
                sub = emit_yaml(v, ind + 1)
                out.append(f"{pad}- {sub[0][len(pad) + 2:]}")
                out += sub[1:]
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}- |")
                out += [f"{pad}  {ln}" if ln else "" for ln in v.rstrip("\n").split("\n")]
            else:
                out.append(f"{pad}- {_scalar(v)}")
    return out


def render_sdl(s: dict[str, Any], image: str, pubkey: str, ports: list[int] | None = None) -> str:
    d = build_sdl(s, image, pubkey, ports)
    bad = validate_sdl(d)
    if bad:
        raise SpecError("invalid SDL:\n  " + "\n  ".join(bad))
    return "# Rendered by `ddc job`. Contains only the PUBLIC ssh key; no secrets.\n" + "\n".join(emit_yaml(d)) + "\n"
