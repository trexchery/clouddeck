"""Strict generator and validator for the hardware part of the SDL.

A profile (config.toml or custom_profiles.json) is a plain dict.  Everything that ends up in the SDL passes through the checks below first:
tokens must match a fixed grammar, numbers are range-checked, and the output is built from fixed line templates, so no text typed in a form
can add a key, a quote, a comment or a newline.  Nothing here knows a GPU model, vendor, CPU architecture or region: those are only
*shaped* (lower-case letters, digits and . _ -), so a model that appears on the network tomorrow works without a code change.

No network, no config, no controller import: `controller.render_sdl` and the tests call it with a dict."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
RAM_RE = re.compile(r"[1-9]\d{0,3}(Gi|Mi)")
ADDR_RE = re.compile(r"akash1[0-9A-Za-z]{4,60}")       # shape only: an address is a bid filter / signer, never free text
SIZE_RE = re.compile(r"\d{1,6}(\.\d{1,3})?(Mi|Gi|Ti)")
INTERFACES = ("pcie", "sxm", "mig")
MAX_ANY_OF = 8          # models in one "any of" list
MAX_PINS = 16           # provider addresses in one pin list
MAX_SIGNERS = 4

# Absolute maxima (the Akash SDL / API limits); config.toml may lower the defaults below but never exceed these.
ABS_MAX = {"max_cpu": 384, "max_mem_gb": 2048, "max_disk_gb": 32768, "max_gpu": 24}
DEFAULT_MAX = {"max_cpu": 128, "max_mem_gb": 1024, "max_disk_gb": 4000, "max_gpu": 8}      # what earlier versions allowed; fat-finger bounds


class SdlError(ValueError):
    """The profile cannot be turned into a safe SDL."""


def tok(v: Any, what: str) -> str:
    """A lower-case id token (vendor, model, interface, cpu arch, ...).  Case is folded because the network ids are lower-case; spaces, quotes,
    colons, newlines or anything else raise."""
    s = str(v if v is not None else "").strip().lower()
    if not TOKEN_RE.fullmatch(s):
        raise SdlError(f"{what} must be 1-64 characters of a-z 0-9 . _ - (no spaces), got {str(v)[:40]!r}")
    return s


def norm_model(name: str) -> str:
    """A GPU model id for the SDL: a token-shaped id stays as typed (lower-cased), 'RTX 4090' becomes 'rtx4090'."""
    t = (name or "").strip().lower()
    return t if TOKEN_RE.fullmatch(t) else re.sub(r"[^a-z0-9]", "", t)


def ram(v: Any, what: str = "gpu_ram") -> str:
    s = str(v if v is not None else "").strip()
    if not RAM_RE.fullmatch(s):
        raise SdlError(f"{what} must look like 24Gi (a whole number of Gi or Mi, exact case), got {s[:20]!r}")
    return s


def addr(v: Any, what: str) -> str:
    s = str(v if v is not None else "").strip()
    if not ADDR_RE.fullmatch(s):
        raise SdlError(f"{what} must be an akash1... address, got {s[:30]!r}")
    return s


def _scalar(s: str) -> str:
    """A token as a YAML scalar: quoted when a YAML parser could read it as a non-string (a digit-leading token such as 0x1f, 0o7, 0b1, 1e3, 12;
    the bool / null words of YAML 1.1 and 1.2), so a model or interface is always a string (tokens contain no quote characters)."""
    return f'"{s}"' if re.match(r"[0-9._-]", s) or s in ("true", "false", "null", "yes", "no", "on", "off", "y", "n") else s


def _list(pr: Mapping[str, Any], key: str, limit: int, what: str) -> list[Any]:
    v = pr.get(key)
    if v in (None, "", ()):
        return []
    if not isinstance(v, (list, tuple)):
        raise SdlError(f"{what} must be a list")
    if len(v) > limit:
        raise SdlError(f"{what} may hold at most {limit} entries")
    return list(v)


def gpu_models(pr: Mapping[str, Any]) -> list[str]:
    """The accepted models in order: `gpu_model` first, then the extra `gpu_models` alternatives (any of them is fine)."""
    out: list[str] = []
    first = pr.get("gpu_model")
    if first not in (None, ""):
        out.append(tok(first, "gpu_model"))
    for m in _list(pr, "gpu_models", MAX_ANY_OF, "gpu_models"):
        t = tok(m, "gpu_models entry")
        if t not in out:
            out.append(t)
    if len(out) > MAX_ANY_OF:
        raise SdlError(f"at most {MAX_ANY_OF} GPU models in one order")
    return out


def hardware_problems(pr: Mapping[str, Any], limits: Mapping[str, Any] | None = None) -> list[str]:
    """Every problem with the hardware fields of a profile (empty = renders safely).  `limits` may lower the numeric bounds
    (max_cpu, max_mem_gb, max_disk_gb, max_gpu); unknown keys in `pr` are ignored (forward compatible)."""
    out: list[str] = []
    lim = bounds(limits)
    cpu, gpu = pr.get("cpu"), pr.get("gpu", 0)
    if not (isinstance(cpu, int) and not isinstance(cpu, bool) and 1 <= cpu <= lim["max_cpu"]):
        out.append(f"cpu must be a whole number from 1 to {lim['max_cpu']}")
    if not (isinstance(gpu, int) and not isinstance(gpu, bool) and 0 <= gpu <= lim["max_gpu"]):
        out.append(f"gpu must be a whole number from 0 to {lim['max_gpu']}")
    for k, cap in (("mem", lim["max_mem_gb"]), ("disk", lim["max_disk_gb"])):
        v = pr.get(k)
        if isinstance(v, str) and SIZE_RE.fullmatch(v) and _gib(v) > cap:
            out.append(f"{k} {v} is above the limit of {cap} Gi (config.toml max_{k}_gb)")
    if not isinstance(gpu, int) or isinstance(gpu, bool) or gpu == 0:
        if any(pr.get(k) for k in ("gpu_vendor", "gpu_ram", "gpu_interface", "gpu_models")):   # (a bare gpu_model with gpu = 0 was always ignored: still is)
            out.append("GPU vendor / VRAM / interface / models need gpu >= 1")
        return out + placement_problems(pr)
    return out + extension_problems(pr, True)


def placement_problems(pr: Mapping[str, Any], need_signers: bool = True) -> list[str]:
    """Problems with the placement fields (CPU arch / vendor, region, provider pins, auditor).  A Spec that only says `audited_only` has no
    auditor address yet (the provider adds it when the order is prepared), so it passes need_signers=False."""
    out = []
    for fn in (placement_block if need_signers else placement_attrs, pins):
        try:
            fn(pr)
        except SdlError as e:
            out.append(str(e))
    return out


def extension_problems(pr: Mapping[str, Any], need_signers: bool = False) -> list[str]:
    """Problems with the GPU and placement fields of an order with gpu >= 1."""
    out = []
    try:
        gpu_attrs(pr)
    except SdlError as e:
        out.append(str(e))
    return out + placement_problems(pr, need_signers)


def _gib(size: str) -> float:
    n = float(size[:-2])
    return n / 1024 if size.endswith("Mi") else n * 1024 if size.endswith("Ti") else n


def bounds(limits: Mapping[str, Any] | None) -> dict[str, int]:
    """Numeric bounds: the defaults, lowered or raised by config.toml within the absolute maxima.  A malformed value is ignored (default)."""
    out = dict(DEFAULT_MAX)
    for k in out:
        v = (limits or {}).get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= ABS_MAX[k]:
            out[k] = v
    return out


def gpu_attrs(pr: Mapping[str, Any]) -> dict[str, Any]:
    """{vendor, models, ram, interface} of the GPU request (strings are '' = unspecified).  Raises SdlError on a bad token."""
    models = gpu_models(pr)
    vendor = tok(pr["gpu_vendor"], "gpu_vendor") if pr.get("gpu_vendor") else "nvidia"
    iface = tok(pr["gpu_interface"], "gpu_interface") if pr.get("gpu_interface") else ""
    vram = ram(pr["gpu_ram"]) if pr.get("gpu_ram") else ""
    if (vram or iface) and not models:
        raise SdlError("a GPU VRAM or interface constraint needs a model (an order for any model of a vendor cannot pin them)")
    return {"vendor": vendor, "models": models, "ram": vram, "interface": iface}


def gpu_block(pr: Mapping[str, Any]) -> str:
    """The SDL `gpu:` block (8-space indent, inside resources) for a profile with gpu >= 1; '' otherwise.  For a profile without the extended
    keys the text is exactly what earlier versions rendered."""
    n = pr.get("gpu", 0) or 0
    if not (isinstance(n, int) and not isinstance(n, bool)) or n < 0:
        raise SdlError("gpu must be a whole number")
    if n == 0:
        return ""
    a = gpu_attrs(pr)
    lines = ["        gpu:", f"          units: {n}", "          attributes:", "            vendor:", f"              {_scalar(a['vendor'])}:"]
    for m in a["models"]:
        lines.append(f"                - model: {_scalar(m)}")
        if a["ram"]:
            lines.append(f"                  ram: {a['ram']}")
        if a["interface"]:
            lines.append(f"                  interface: {_scalar(a['interface'])}")
    return "\n".join(lines) + "\n"


def placement_attrs(pr: Mapping[str, Any]) -> dict[str, str]:
    """Provider attributes the order requires (SDL `placement.<name>.attributes`).  A provider that does not state an attribute cannot bid."""
    out: dict[str, str] = {}
    if pr.get("cpu_vendor"):
        out["capabilities/cpu"] = tok(pr["cpu_vendor"], "cpu_vendor")
    if pr.get("cpu_arch"):
        out["capabilities/cpu/arch"] = tok(pr["cpu_arch"], "cpu_arch")
    if pr.get("region"):
        out["location-region"] = tok(pr["region"], "region")
    return out


def signers(pr: Mapping[str, Any]) -> list[str]:
    """Auditor addresses the providers must be signed by (`audited_only`).  The audited flag without an auditor is refused: an order that
    silently drops the requirement is worse than none."""
    sig = [addr(a, "signed_by entry") for a in _list(pr, "signed_by", MAX_SIGNERS, "signed_by")]
    flag = pr.get("audited_only")
    if flag not in (None, True, False):
        raise SdlError("audited_only must be true or false")
    if flag and not sig:
        raise SdlError("audited_only needs the auditor address (signed_by): it is read from the network when the order is prepared")
    return sig


def placement_block(pr: Mapping[str, Any]) -> str:
    """Lines (6-space indent) for `profiles.placement.akash`: attributes and signedBy; '' when the profile asks for neither."""
    lines: list[str] = []
    attrs = placement_attrs(pr)
    if attrs:
        lines.append("      attributes:")
        lines += [f'        {k}: "{v}"' for k, v in attrs.items()]
    sig = signers(pr)
    if sig:
        lines += ["      signedBy:", "        anyOf:"] + [f'          - "{a}"' for a in sig]
    return "\n".join(lines) + "\n" if lines else ""


def pins(pr: Mapping[str, Any]) -> list[str]:
    """Provider owners a bid must come from (client-side bid filter, not an SDL feature)."""
    return [addr(a, "providers entry") for a in _list(pr, "providers", MAX_PINS, "providers")]


def screening_request(pr: Mapping[str, Any], cap_amount: int) -> dict[str, Any]:
    """The body of POST /v1/bid-screening for the same order the SDL describes: capacity and attribute fit only, no price comes back."""
    n = pr.get("gpu", 0) or 0
    cpu, mem, disk = pr.get("cpu"), pr.get("mem"), pr.get("disk")
    if not (isinstance(cpu, int) and not isinstance(cpu, bool) and cpu >= 1):
        raise SdlError("cpu must be a whole number")
    for k, v in (("mem", mem), ("disk", disk)):
        if not (isinstance(v, str) and SIZE_RE.fullmatch(v)):
            raise SdlError(f"{k} must look like 32Gi")
    gpu_attr: list[dict[str, str]] = []
    if n:
        a = gpu_attrs(pr)
        for m in a["models"] or ["*"]:
            key = f"vendor/{a['vendor']}/model/{m}" + (f"/ram/{a['ram']}" if a["ram"] else "") + (f"/interface/{a['interface']}" if a["interface"] else "")
            gpu_attr.append({"key": key, "value": "true"})
    req: dict[str, Any] = {"attributes": [{"key": k, "value": v} for k, v in placement_attrs(pr).items()]}
    if signers(pr):
        req["signedBy"] = {"allOf": [], "anyOf": signers(pr)}
    return {"timezone": "UTC", "requirements": req, "resources": [{
        "count": 1, "price": {"denom": "uact", "amount": str(int(cap_amount))},
        "resource": {"id": 1, "cpu": {"units": {"val": str(cpu * 1000)}, "attributes": []},
                     "memory": {"quantity": {"val": str(int(_gib(mem) * 2**30))}, "attributes": []},
                     "gpu": {"units": {"val": str(n)}, "attributes": gpu_attr},
                     "storage": [{"name": "default", "quantity": {"val": str(int(_gib(disk) * 2**30))}, "attributes": []}], "endpoints": []}}]}


def check_sdl(text: str) -> None:
    """Last line of defence on the rendered text (no YAML parser at run time): version, a single integer price cap of at least 1 uact per
    block (the order is always capped), and no placeholder left over.  Raises SdlError."""
    body = re.sub(r"(?m)^\s*#.*$", "", text)
    if "__" in body:
        raise SdlError("unrendered SDL placeholder")
    if not re.search(r'(?m)^version: "2\.0"$', text):
        raise SdlError("SDL version line missing")
    m = re.findall(r"(?m)^          denom: uact\n          amount: (\d+)$", text)
    if len(m) != 1 or int(m[0]) < 1:
        raise SdlError("the SDL must carry exactly one price cap of at least 1 uact per block")


def single_price(text: str) -> int:
    """The one price (uact per block) a rendered SDL orders at, read back from the text itself, for the code that is about to POST it.  A small strict
    reader for the SDLs this code base generates (the controller template and `ddc job`): it refuses an SDL whose pricing it cannot read exactly,
    one with more than one `denom`/`amount` entry (more than one priced service), a denom other than uact, or a service `count` other than 1.
    Raises SdlError."""
    body = re.sub(r"(?m)^\s*#.*$", "", text)
    denoms = re.findall(r'(?m)^[ ]+denom:[ ]*"?([A-Za-z0-9/]+)"?[ ]*$', body)
    amounts = re.findall(r'(?m)^[ ]+amount:[ ]*"?(\d+)"?[ ]*$', body)
    if denoms != ["uact"] or len(amounts) != 1:
        raise SdlError("the SDL must carry exactly one price, in uact")
    if any(c != "1" for c in re.findall(r'(?m)^[ ]+count:[ ]*(\S+)[ ]*$', body)):
        raise SdlError("the SDL must order exactly one instance (count: 1) of one service")
    return int(amounts[0])
