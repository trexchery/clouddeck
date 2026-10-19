"""Akash market side of a job: create deployment, collect and rank bids, lease, find the SSH endpoint.

Everything goes through the controller: the deployment is created by `ddc.launch_lease` (the same spending gate, write-ahead record and
single POST as every other lease), the Console API client is `ddc.api`, and the lease is closed only by `ddc.close_box`.  Every API call goes
through `ddc.api`/`ddc.public_get`, which tests replace with a stub.
"""
from __future__ import annotations

import datetime as dt
import math
import secrets
import statistics
import urllib.parse
from collections.abc import Callable
from typing import Any

from clouddeck import controller as ddc

from .preflight import limit_h, spend_label
from .spec import SERVICE


class ProvisionFailed(RuntimeError):
    """This attempt cannot produce a ready box. `image_related` (SSH never came up) means the stock image may do better;
    otherwise (no bids) the same image is simply retried once."""

    def __init__(self, msg: str, image_related: bool = True):
        super().__init__(msg)
        self.image_related = image_related


class History:
    """Per (provider, image) boot history, used as a cheap image-cache-hit heuristic.

    Akash providers cache pulled layers per node, so a provider that already booted this exact image tag is likely to
    start it faster next time. We store the measured lease->sshd seconds and fold them into bid ranking as expected cost.
    """

    BLOCK_S = 24 * 3600

    def __init__(self, path, clock):
        self.path, self.clock = path, clock
        self.d: dict[str, Any] = ddc.read_json(path, {})

    def rec(self, prov: str, image: str) -> dict[str, Any]:
        return (self.d.get(prov) or {}).get(image) or {}

    def boot_estimate(self, prov: str, image: str, default: float) -> float:
        b = self.rec(prov, image).get("boot_s") or []
        return float(statistics.median(b)) if b else default

    def blocked(self, prov: str, image: str) -> bool:
        return float(self.rec(prov, image).get("bad_until") or 0) > self.clock.time()

    def record(self, prov: str, image: str, ok: bool, boot_s: float | None = None) -> None:
        r = self.d.setdefault(prov, {}).setdefault(image, {})
        if ok:
            r["ok"] = int(r.get("ok", 0)) + 1
            r["last_ok"] = self.clock.time()
            if boot_s is not None:
                r["boot_s"] = (list(r.get("boot_s") or []) + [round(boot_s, 1)])[-5:]
            r.pop("bad_until", None)
        else:
            r["fail"] = int(r.get("fail", 0)) + 1
            r["bad_until"] = self.clock.time() + self.BLOCK_S
        ddc.write_json(self.path, self.d)


def rank_bids(bids: list[dict[str, Any]], spec: dict[str, Any], hist: History, image: str, clock, cold_boot_s: float | None = None) -> list[dict[str, Any]]:
    """Open bids from healthy providers within the price cap, cheapest expected total cost first.

    expected cost = $/h x (job seconds + expected boot seconds): a slightly dearer provider that already has the image
    cached beats a cheaper one that must pull it, exactly when the pull costs more than the price difference.
    """
    pinfo = ddc.providers()
    perf = ddc.read_json(ddc.PERF, {})
    job_s = max(60.0, spec["estimated_runtime_min"] * 60)
    cold_boot_s = spec["cold_boot_s"] if cold_boot_s is None else cold_boot_s
    cap = float(spec["hardware"]["max_usd_hr"] or 0)
    out = []
    for b in bids:
        bid = b.get("bid") or {}
        prov = (bid.get("id") or {}).get("provider")
        if bid.get("state") != "open" or not prov:
            continue
        p = pinfo.get(prov) or {}
        if p and float(p.get("uptime7d") or 0) < spec["hardware"]["min_uptime"]:
            continue
        if float((perf.get(prov) or {}).get("bad_until") or 0) > clock.time() or hist.blocked(prov, image):
            continue
        usd_hr = ddc.bid_usd_hr(bid.get("price"))   # the controller's own price check: uact only, a finite amount above zero (a NaN price would blind the budget guard)
        if usd_hr is None:
            continue
        if usd_hr > cap + 1e-6:  # the SDL price cap already keeps these out; a bid above the user's cap is never leased
            continue
        boot = hist.boot_estimate(prov, image, cold_boot_s)
        out.append({"provider": prov, "usd_hr": usd_hr, "id": bid["id"], "boot_s": boot, "known": bool(hist.rec(prov, image).get("ok")),
                    "host": (p.get("hostUri") or "").split("//")[-1].split(":")[0], "exp_cost": usd_hr * (job_s + boot)})
    return sorted(out, key=lambda r: (r["exp_cost"], r["usd_hr"]))


class Market:
    def __init__(self, spec: dict[str, Any], clock, tel, hist: History, log):
        self.s, self.clock, self.tel, self.hist, self.log = spec, clock, tel, hist, log
        self.dseq: str | None = None
        self.manifest: Any = None
        self.name = ""
        self.image = ""

    @property
    def limit_h(self) -> int:
        return limit_h(self.s)

    def create(self, sdl: str, image: str) -> str:
        """Create the deployment through the controller's single creation path (spending gate, write-ahead record, POST once).  Held under the
        cross-process lock so the watchdog / `ddc reconcile` can never act on the write-ahead record while it is being resolved."""
        self.image = image
        self.name = f"{ddc.NAME_PREFIX}job-{self.s['name']}-{dt.datetime.fromtimestamp(self.clock.time()):%m%d%H%M}-{secrets.token_hex(2)}"  # unique: reconcile finds it by name
        with ddc.FileLock(wait_s=300):
            created = ddc.launch_lease(self.name, sdl, role="job", limit_h=self.limit_h, cap_usd_hr=float(self.s["hardware"]["max_usd_hr"] or 0),
                                       label=spend_label(self.s), fields={"job": self.s["name"], "image": image, "heartbeat_at": self.clock.time()})
        self.dseq, self.manifest = str(created["dseq"]), created.get("manifest")
        self.tel.mark("create")
        self.log(f"{self.name}: created dseq={self.dseq} (runtimeLimitHours={self.limit_h}); collecting bids")
        return self.dseq

    def bids(self) -> list[dict[str, Any]]:
        raw = ddc.api("GET", "/v1/bids?" + urllib.parse.urlencode({"dseq": self.dseq})).get("data") or []
        ranked = rank_bids(raw, self.s, self.hist, self.image, self.clock)
        if ranked:
            self.tel.mark("bid")
        return ranked

    def lease(self, choice: dict[str, Any]) -> None:
        bid_id = choice["id"]
        price = choice.get("usd_hr")
        if not (isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and 0 < price <= float(self.s["hardware"]["max_usd_hr"] or 0) + 1e-6):
            raise ddc.DdcError(f"refusing to lease at a price of {price!r} $/h: it must be a finite number above zero and within the spec's cap")
        body = {"manifest": self.manifest, "leases": [{"dseq": self.dseq, "gseq": bid_id["gseq"], "oseq": bid_id["oseq"], "provider": bid_id["provider"]}]}
        for attempt in range(3):
            try:
                ddc.api("POST", "/v1/leases", body, timeout=120)
                break
            except ddc.ApiUncertain:  # may or may not have gone through: look, never post it blindly again
                if ddc.deployment(self.dseq).get("leases"):
                    break
                raise
            except ddc.DdcError as e:
                if "manifest_not_delivered" not in str(e) or attempt == 2:
                    raise
                self.clock.sleep(10)
        self.tel.mark("lease")
        self.tel.mark("manifest")  # the lease call carries the manifest; the provider starts pulling the image from here
        t = self.clock.time()
        ddc.update_dep(self.dseq, status="leased", provider=choice["provider"], provider_host=choice["host"], usd_hr=choice["usd_hr"], lease_at=t,
                       deadline_at=t + float(self.s["runtime_hours"]) * 3600, heartbeat_at=t)
        ddc.ledger_add({"event": "leased", "dseq": self.dseq, "provider": choice["provider"], "usd_hr": choice["usd_hr"]})
        self.log(f"leased {choice['host'] or choice['provider']} at ${choice['usd_hr']:.3f}/h "
                 f"({'image known there' if choice.get('known') else 'image not seen there before'})")

    def endpoint(self, timeout_s: float = 400, beat: Callable[[], None] | None = None) -> tuple[str, int]:
        """(host, port) of the box's ssh port as the provider reports it.  Both are provider-controlled and end up in ssh arguments, so they are
        checked with the controller's valid_host / valid_port; an unusable endpoint aborts the attempt (the runner closes the lease)."""
        t_end = self.clock.time() + timeout_s
        while self.clock.time() < t_end:
            if beat:
                beat()   # up to timeout_s of waiting: the runner must keep saying it is alive
            det = ddc.deployment(self.dseq)
            for lease in det.get("leases") or []:
                st = lease.get("status") or {}
                sv = (st.get("services") or {}).get(SERVICE) or {}
                if int(sv.get("ready_replicas") or sv.get("available") or 0) > 0:
                    self.tel.mark("container_ready")
                for lst in (st.get("forwarded_ports") or {}).values():
                    for fp in lst:
                        if int(fp.get("port") or 0) == 22:
                            self.tel.mark("container_ready")  # fallback when the provider reports no replica counts
                            host, port = fp.get("host"), fp.get("externalPort")
                            if not ddc.valid_host(host) or not ddc.valid_port(port):
                                self._distrust_provider()
                                raise ddc.DdcError(f"the provider returned an unusable ssh endpoint {host!r}:{port!r}; closing the lease")
                            return str(host), int(port)
            self.clock.sleep(4)
        raise ProvisionFailed(f"lease exposed no ssh port within {timeout_s:.0f}s")

    def _distrust_provider(self) -> None:
        try:
            prov = ddc.load_state()["deps"][self.dseq].get("provider")
            if prov:
                perf = ddc.read_json(ddc.PERF, {})
                perf.setdefault(prov, {})["bad_until"] = self.clock.time() + 24 * 3600
                ddc.write_json(ddc.PERF, perf)
        except Exception:  # noqa: BLE001  (bookkeeping only: the lease is closed either way)
            pass
