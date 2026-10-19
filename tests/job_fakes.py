"""Stubbed Akash API, box and local prep on a virtual clock for the `ddc job` tests and tests/job_sim.py (the simulation).

Nothing here touches the network or spends money. Latencies are parameters; the simulation labels any number that comes
from them as ESTIMATED unless the parameter itself is a measured value.  GPU/provider names are made-up fixtures.

    install(monkeypatch, clock, akash)     # wires the controller to the fake API + virtual clock (state/secrets are already isolated by conftest)
    akash.fail_next("POST /v1/deployments", "timeout_after")   # executed, but the caller sees ApiUncertain (also: timeout_before, reject)
    akash.next_host = "bad host; rm -rf /"                      # what the provider reports as the ssh host of the next lease
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
import time as _time
import types
from pathlib import Path
from typing import Any

from clouddeck.engine import ddc
from clouddeck.job.clock import FakeClock

INF = float("inf")


class FakeAkash:
    """Console API subset used by the controller and `ddc job`. Only reads, create-deployment, create-lease, list and close are implemented."""

    def __init__(self, clock: FakeClock, *, providers: list[dict[str, Any]] | None = None, bid_delay_s: float = 40, port_delay_s: float = 15,
                 boot_s: dict[str, float] | None = None, balance_usd: float = 100.0, pull_s: float = 30, lease_latency_s: float = 0.0):
        self.clock, self.bid_delay_s, self.port_delay_s, self.pull_s = clock, bid_delay_s, port_delay_s, pull_s
        self.lease_latency_s = lease_latency_s
        gm = [{"model": "model-a", "ram": "80Gi"}, {"model": "model-b", "ram": "24Gi"}]
        self.providers = providers or [{"owner": "akash1cheap", "usd_hr": 2.0, "uptime7d": 0.99, "gpuModels": gm},
                                      {"owner": "akash1mid", "usd_hr": 2.4, "uptime7d": 0.99, "gpuModels": gm},
                                      {"owner": "akash1flaky", "usd_hr": 1.0, "uptime7d": 0.5, "gpuModels": gm}]
        self.boot_s = boot_s or {"default": 50.0}  # seconds from lease to sshd ready, per image (key = substring of image) or "default"
        self.balance = balance_usd
        self.deps: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.posts: list[dict[str, Any]] = []   # body of every POST /v1/deployments (the SDL text, name, runtimeLimitHours)
        self.next_host: str | None = None
        self._fail: list[list[Any]] = []
        self._n = 0

    # ---- helpers
    def boot_for(self, image: str) -> float:
        return next((v for k, v in self.boot_s.items() if k != "default" and k in image), self.boot_s["default"])

    def ready_at(self, dseq: str) -> float:
        d = self.deps[dseq]
        return INF if d.get("lease_at") is None or d["boot"] == INF else d["lease_at"] + d["boot"]

    def active_leases(self) -> list[str]:
        return [k for k, d in self.deps.items() if d["state"] == "active" and d.get("lease_at") is not None]

    def cost(self, dseq: str) -> float:
        d = self.deps[dseq]
        if d.get("lease_at") is None:
            return 0.0
        return ((d.get("closed_at") or self.clock.time()) - d["lease_at"]) / 3600 * d["usd_hr"]

    def fail_next(self, route: str, kind: str, times: int = 1) -> None:
        """route = "METHOD /path-prefix". kind: timeout_after (executed, caller sees ApiUncertain), timeout_before (not executed), reject (HTTP 400)."""
        self._fail.append([route, kind, times])

    def _take_fail(self, key: str) -> str | None:
        for f in self._fail:
            if key.startswith(f[0]) and f[2] > 0:
                f[2] -= 1
                return f[1]
        return None

    def add_foreign(self, dseq: str = "1000000000001", name: str = "other-service+db+web") -> str:
        """A deployment this tool never created (not ddc-named, not in state): active and leased, so it appears in spend-rate."""
        self.deps[dseq] = {"name": name, "state": "active", "created": self.clock.time(), "image": "x", "limit": None, "boot": 0.0, "lease_at": self.clock.time(),
                           "closed_at": None, "provider": "akash1cheap", "usd_hr": 0.5}
        return dseq

    # ---- ddc.public_get
    def public_get(self, path: str) -> Any:
        self.calls.append(("GET", path))
        if path == "/v1/providers":
            return [{"owner": p["owner"], "hostUri": f"https://{p['owner']}.example:8443", "isOnline": True, "uptime7d": p["uptime7d"],
                     "gpuModels": p.get("gpuModels") or [], "stats": {"gpu": {"available": p.get("gpu_free", 4), "total": 8}}, "attributes": []}
                    for p in self.providers]
        if path == "/v1/gpu-prices":
            return {"models": [{"model": "model-a", "ram": "80Gi", "interface": "sxm", "price": {"min": 1.8, "med": 2.2, "max": 3.0},
                                "availability": {"available": 6, "total": 16}}]}
        raise AssertionError(f"unexpected public GET {path}")

    # ---- ddc.api
    def api(self, method: str, path: str, body: Any = None, timeout: int = 60, ok404: bool = False) -> Any:
        kind = self._take_fail(f"{method} {path.split('?')[0]}")
        if kind == "reject":
            self.calls.append((method, path))
            raise ddc.DdcError(f"HTTP 400 {method} {path}: rejected")
        if kind == "timeout_before":
            self.calls.append((method, path))
            raise ddc.ApiUncertain(f"{method} {path}: timed out")
        out = self._api(method, path, body, timeout, ok404)
        if kind == "timeout_after":
            raise ddc.ApiUncertain(f"{method} {path}: timed out after executing")
        return out

    def _api(self, method: str, path: str, body: Any = None, timeout: int = 60, ok404: bool = False) -> Any:
        self.calls.append((method, path))
        t = self.clock.time()
        if method == "GET" and path == "/v1/balances":
            return {"data": {"total": self.balance * 1e6}}
        if method == "POST" and path == "/v1/deployments":
            self._n += 1
            dseq = str(1_000_000 + self._n)
            data = body["data"]
            self.posts.append(data)
            assert data.get("runtimeLimitHours"), "runtimeLimitHours must always be sent"
            image = re.search(r'image: "([^"]+)"', data["sdl"]).group(1)
            self.deps[dseq] = {"name": data["name"], "state": "active", "created": t, "image": image, "limit": data.get("runtimeLimitHours"),
                               "boot": self.boot_for(image), "lease_at": None, "closed_at": None}
            return {"data": {"dseq": dseq, "manifest": {"m": dseq}}}
        if method == "GET" and path.startswith("/v1/bids?"):
            dseq = path.split("dseq=")[1]
            d = self.deps[dseq]
            if t - d["created"] < self.bid_delay_s:
                return {"data": []}
            return {"data": [{"bid": {"id": {"provider": p["owner"], "dseq": dseq, "gseq": 1, "oseq": 1}, "state": "open",
                                      "price": {"denom": "uact", "amount": str(int(p["usd_hr"] * 1e6 / 600))}}} for p in self.providers]}
        if method == "POST" and path == "/v1/leases":
            ls = body["leases"][0]
            d = self.deps[ls["dseq"]]
            assert body.get("manifest"), "manifest missing"
            prov = next(p for p in self.providers if p["owner"] == ls["provider"])
            self.clock.sleep(self.lease_latency_s)  # manifest delivery to the provider
            d.update(lease_at=self.clock.time(), provider=prov["owner"], usd_hr=prov["usd_hr"])
            return {"data": {}}
        if method == "GET" and path.startswith("/v1/deployments?"):
            rows = [{"name": d["name"], "deployment": {"id": {"dseq": k, "owner": "akash1fakeowner"}}} for k, d in self.deps.items() if d["state"] == "active"]
            return {"data": {"deployments": rows}}
        m = path.split("/v1/deployments/")
        if method == "GET" and len(m) == 2:
            d = self.deps.get(m[1])
            if d is None:
                return None if ok404 else {}
            det: dict[str, Any] = {"name": d["name"], "deployment": {"state": "closed" if d["state"] == "closed" else "active"},
                                   "leases": [], "escrow_account": {"state": {"transferred": [{"amount": str(self.cost(m[1]) * 1e6)}]}}}
            if d.get("lease_at") is not None and t - d["lease_at"] >= self.port_delay_s and d["state"] == "active":
                host = self.next_host if self.next_host is not None else f"{d['provider']}.example"
                det["leases"] = [{"status": {"forwarded_ports": {"job": [{"port": 22, "host": host, "externalPort": 30022}]}}}]
            return {"data": det}
        if method == "DELETE" and len(m) == 2:
            d = self.deps[m[1]]
            d["state"], d["closed_at"] = "closed", t
            return {"data": {}}
        if method == "GET" and path == "/v1/spend-rate":
            return {"data": {"deployments": [{"dseq": k} for k in self.active_leases()]}}
        raise AssertionError(f"unexpected API call {method} {path}")


class FakeRemote:
    """In-memory box with the SshRemote interface. Step timing is resolved lazily from launch/needs/duration tables."""

    def __init__(self, clock: FakeClock, akash: FakeAkash, *, up_bps: float = 100e6, down_bps: float = 100e6, rtt: float = 0.3,
                 durations: dict[str, float] | None = None, produces: dict[str, dict[str, bytes]] | None = None,
                 fail: dict[str, int] | None = None, drop_after: int | None = None):
        self.clock, self.akash, self.up_bps, self.down_bps, self.rtt = clock, akash, up_bps, down_bps, rtt
        self.durations, self.produces, self.fail = durations or {}, produces or {}, fail or {}
        self.dseq: str | None = None
        self.files: dict[str, bytes] = {}
        self.parts: dict[str, bytearray] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.marks: dict[str, tuple[float, float]] = {"": (0, 0)}
        self.resolved: dict[str, tuple[float, float]] = {}
        self.secrets: dict[str, str] = {}
        self.offset = 0.0
        self.calls = 0
        self.drop_after, self.dropped = drop_after, False  # simulate one transient ssh outage after N calls

    def _rt(self) -> None:
        self.calls += 1
        self.clock.sleep(self.rtt)

    # ---- lifecycle
    def probe(self) -> bool:
        self._rt()
        return self.akash.ready_at(self.dseq) <= self.clock.time()

    def boot_info(self) -> dict[str, Any]:
        self._rt()
        d = self.akash.deps[self.dseq]
        start = d["lease_at"] + self.akash.pull_s
        apt = 0.0 if "prebuilt" in d["image"] else max(0.0, d["boot"] - self.akash.pull_s - 5)
        return {"start": start, "toolchain_done": start + apt, "installed": "" if apt == 0 else "aria2 rsync"}

    def install_scripts(self) -> None:
        self._rt()

    def push_secrets(self, env: dict[str, str]) -> None:
        self._rt()
        self.secrets |= env

    # ---- steps
    def start(self, name: str, needs: list[str], cmd: str) -> None:
        self._rt()
        if name not in self.steps and name not in self.resolved:
            self.steps[name] = {"needs": needs, "cmd": cmd, "launched": self.clock.time()}

    def mark_done(self, name: str) -> None:
        self._rt()
        self.marks.setdefault(name, (self.clock.time(), self.clock.time()))

    def _resolve(self) -> None:
        changed = True
        while changed:
            changed = False
            for n, st in list(self.steps.items()):
                ends = [self.marks[x][1] if x in self.marks else self.resolved[x][1] if x in self.resolved else None for x in st["needs"]]
                if any(e is None for e in ends):
                    continue
                start = max([st["launched"], *ends])
                self.resolved[n] = (start, start + self.durations.get(n, 1.0))
                del self.steps[n]
                changed = True

    def markers(self) -> dict[str, dict[str, Any]]:
        self._rt()
        self._resolve()
        now, out = self.clock.time(), {}
        for n, (s, e) in self.resolved.items():
            if now >= e:
                rc = self.fail.get(n, 0)
                out[n] = {"state": "done" if rc == 0 else "failed", "rc": rc, "start": s, "end": e}
                if rc == 0:
                    for p, b in self.produces.get(n, {}).items():
                        self.files[p] = b
            elif now >= s:
                out[n] = {"state": "running", "start": s}
        return out

    def tail_log(self, name: str, n: int = 15) -> str:
        return f"(fake log of {name})"

    # ---- transfers
    def size(self, path: str) -> tuple[int, bool]:
        self._rt()
        if path in self.files:
            return len(self.files[path]), True
        return len(self.parts.get(path, b"")), False

    def put(self, path: str, offset: int, data: bytes) -> bool:
        self._rt()
        if self.drop_after is not None and not self.dropped and self.calls >= self.drop_after:
            self.dropped = True
            self.parts[path] = bytearray(self.parts.get(path, b"") + data[: len(data) // 2])  # connection died mid-chunk
            self.clock.sleep(len(data) / 2 / self.up_bps)
            return False
        part = self.parts.setdefault(path, bytearray())
        if len(part) != offset:
            return False
        part += data
        self.clock.sleep(len(data) / self.up_bps)
        return True

    def finalize(self, path: str, sha: str) -> bool:
        self._rt()
        part = self.parts.get(path)
        if part is None or hashlib.sha256(bytes(part)).hexdigest() != sha:
            return False
        self.files[path] = bytes(self.parts.pop(path))
        return True

    def drop_part(self, path: str) -> None:
        self._rt()
        self.parts.pop(path, None)

    def glob(self, patterns: list[str]) -> list[tuple[str, int, str]]:
        self._rt()
        return [(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in sorted(self.files.items()) if any(fnmatch.fnmatch(p, g) for g in patterns)]

    def get(self, path: str, offset: int, n: int) -> bytes:
        self._rt()
        data = self.files[path][offset: offset + n]
        self.clock.sleep(len(data) / self.down_bps)
        return data


class TimerPrep:
    """Local prep that takes `seconds` of virtual time and then creates its output files."""

    def __init__(self, clock: FakeClock, seconds: float, outputs: dict[Path, bytes] | None = None, rc: int = 0):
        self.clock, self.seconds, self.outputs, self.rc = clock, seconds, outputs or {}, rc
        self.t0: float | None = None
        self._done = False

    def start(self) -> None:
        self.t0 = self.clock.time()

    def done(self) -> bool:
        if not self._done and self.t0 is not None and self.clock.time() >= self.t0 + self.seconds:
            for p, b in self.outputs.items():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b)
            self._done = True
        return self._done

    def stop(self) -> None:
        pass


def install(mp: Any, clock: FakeClock, akash: FakeAkash, quiet: bool = True) -> None:
    """Point the controller at the fake API and the virtual clock (monkeypatch undoes it).  State, secrets, keys and config are the per-test
    isolated ones from tests/conftest.py."""
    mp.setattr(ddc, "api", akash.api)
    mp.setattr(ddc, "public_get", akash.public_get)
    mp.setattr(ddc, "now", clock.time)
    mp.setattr(ddc, "time", types.SimpleNamespace(sleep=clock.sleep, time=clock.time, monotonic=_time.monotonic, strftime=_time.strftime))
    mp.setattr(ddc, "_prov_cache", {})
    if quiet:
        mp.setattr(ddc, "log", lambda m: None)
    (ddc.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=dummy-not-a-real-key\n")
    ddc.KEYS.mkdir(parents=True, exist_ok=True)
    (ddc.KEYS / "access.pub").write_text("ssh-ed25519 AAAATESTPUBLIC test")
    ddc.CFG["budget_guard"] = True
