"""A small, deterministic, in-memory fake of everything the controller (clouddeck.controller) talks to over the network.

It replaces the controller's seams, so the real money paths (create_box, close_box, check_budget, cmd_start/scale/stop, the watchdog)
run unchanged against it with no network, no ssh, no gpg and no sleeping:

    fake = FakeAkash(ddc, monkeypatch).install()      # in a test (tests/conftest.py already isolates config/state/secrets)
    ddc.create_box("worker", "32", 1)                  # -> a fake lease at fake.deployments[...]
    fake.fail_next("POST /v1/deployments", "timeout_after")   # next call: executed, but the caller sees a timeout
    fake.clock += 3600                                  # time only moves when the code sleeps or the test says so

Seams replaced:  ddc.api, ddc.public_get, ddc.ssh (also what put_file / push_lease_scripts use), ddc.now and ddc.time (sleep advances
the fake clock), ddc.verify_checkpoint (stands in for git clone + gpg), ddc.preflight and ddc.watchdog_installed (no real tools needed).
`fake.patch_subprocess()` additionally fakes subprocess.run for the ssh probe/ssh-keygen/gh/schtasks/systemctl/crontab calls of heal and the
installers; unknown commands succeed with empty output.

Failure kinds for fail_next(route, kind, times=1) (route = "METHOD /path-prefix", matched with startswith, query ignored):
  timeout_after / timeout_before  non-idempotent calls raise ApiUncertain, after or without executing the request
  5xx_after                       same as timeout_after (a 5xx the server answered after doing the work)
  reject                          DdcError("HTTP 400 ...") definitive refusal, nothing executed
  empty                           the call "succeeds" with an empty body ({})
  notfound                        404 (None when the caller passed ok404=True)
  error                           DdcError (a GET that failed even after the real api()'s retries)
  interrupt                       KeyboardInterrupt (Ctrl-C while the call is in flight)

Other knobs: balance (USD), bid_uact (price of every bid, uact/block), providers, checkpoint ("ok" | "refuse" | "fail"), verify ("ok" | "fail"),
agent (dict served as /run/ddc/state.json; `now` is filled in unless present), unreachable (hosts whose ssh always fails).
Inspect: deployments (dseq -> record), calls (every api call), ssh_log, open_dseqs(), by_name(name).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time as _time
import types
from typing import Any

FAKE_OWNER = "akash1fakeowner"
T0 = 1_800_000_000.0


class FakeAkash:
    def __init__(self, ddc: Any, monkeypatch: Any, balance: float = 100.0):
        self.ddc, self.mp = ddc, monkeypatch
        self.clock = T0
        self.balance = balance
        self.bid_uact = 500  # $0.30/h
        self.providers = {f"akash1prov{c}": f"prov-{c}.example" for c in "abc"}
        self.deployments: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.ssh_log: list[tuple[str, str]] = []
        self.checkpoint = "ok"
        self.verify = "ok"
        self.agent: dict[str, Any] = {"uptime_min": 5, "idle_min": 0, "active_reason": "cpu", "dist_idle_min": 0, "last_checkpoint": "",
                                      "checkpoint_failed": False, "shutdown_requested": ""}
        self.unreachable: set[str] = set()
        self.drop_archives: set[str] = set()  # workspace names missing from the (verified) manifest
        self.restarted: set[str] = set()  # hosts whose container restarted (heal)
        self.agent_restarts = 0
        self.next_host: str | None = None  # the ssh host the provider reports for the next lease (to test hostile values)
        self._fail: list[list[Any]] = []
        self._seq = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ setup
    def install(self, repos: bool = False, real_preflight: bool = False) -> FakeAkash:
        d, mp = self.ddc, self.mp
        mp.setattr(d, "api", self.api)
        mp.setattr(d, "public_get", self.public_get)
        mp.setattr(d, "ssh", self.ssh)
        mp.setattr(d, "now", lambda: self.clock)
        mp.setattr(d, "time", types.SimpleNamespace(sleep=self.sleep, time=lambda: self.clock, monotonic=_time.monotonic, strftime=_time.strftime))
        mp.setattr(d, "verify_checkpoint", self.verify_checkpoint)
        mp.setattr(d, "watchdog_installed", lambda: True)
        mp.setattr(d, "_prov_cache", {})
        if not real_preflight:
            mp.setattr(d, "preflight", lambda role="dev": [])
        if not repos:
            d.CFG["repos"], d.CFG["dirs"] = [], []
        d.CFG["budget_guard"] = True
        d.KEYS.mkdir(parents=True, exist_ok=True)
        for k in ("access", "worker", "state_deploy", "deploy_web-app", "github_known_hosts", "gitconfig"):
            (d.KEYS / k).write_text(k)
        (d.SECRETS_DIR / "secrets.env").write_text("AKASH_API_KEY=fake-key\nDDC_BACKUP_PASSPHRASE=fake-pass\n")
        return self

    def sleep(self, s: float) -> None:
        self.clock += s

    def patch_subprocess(self) -> FakeAkash:
        self.mp.setattr(subprocess, "run", self.run)
        return self

    # ------------------------------------------------------------------ failure injection
    def fail_next(self, route: str, kind: str, times: int = 1) -> None:
        self._fail.append([route, kind, times])

    def _take_fail(self, key: str) -> str | None:
        for f in self._fail:
            if key.startswith(f[0]) and f[2] > 0:
                f[2] -= 1
                return f[1]
        return None

    # ------------------------------------------------------------------ introspection
    def open_dseqs(self) -> list[str]:
        return [k for k, v in self.deployments.items() if v["state"] == "active"]

    def by_name(self, name: str) -> list[str]:
        return [k for k, v in self.deployments.items() if v["name"] == name]

    def add_foreign(self, dseq: str = "1000000000001", name: str = "other-service+db+web", owner: str = FAKE_OWNER) -> str:
        self.deployments[dseq] = {"name": name, "owner": owner, "state": "active", "provider": "akash1provX", "t0": self.clock, "foreign": True}
        return dseq

    # ------------------------------------------------------------------ Akash Console API
    def public_get(self, path: str) -> Any:
        if path == "/v1/providers":
            return [{"owner": p, "uptime7d": 1.0, "hostUri": f"https://{h}:8443", "attributes": [{"key": "capabilities/memory", "value": "ddr5"}]}
                    for p, h in self.providers.items()]
        raise AssertionError(f"unfaked public_get {path}")

    def _details(self, dseq: str) -> dict[str, Any]:
        v = self.deployments[dseq]
        leases = []
        if v.get("provider") and v["state"] == "active" and v.get("leased"):
            leases = [{"id": {"provider": v["provider"]}, "status": {"forwarded_ports": {"box": [
                {"port": 22, "host": v["host"] if "host" in v else self.providers.get(v["provider"], "h.example"), "externalPort": 30000 + int(dseq) % 1000}]}}}]
        hours = (self.clock - v["t0"]) / 3600
        return {"name": v["name"], "deployment": {"state": v["state"], "id": {"owner": v["owner"], "dseq": dseq}}, "leases": leases,
                "escrow_account": {"state": {"transferred": [{"amount": str(int(hours * v.get("usd_hr", 0.3) * 1e6))}] if v.get("leased") else []}}}

    def api(self, method: str, path: str, body: Any = None, timeout: int = 60, ok404: bool = False) -> Any:
        with self._lock:
            route = path.split("?")[0]
            self.calls.append((method, route))
            kind = self._take_fail(f"{method} {route}")
            uncertain = method not in ("GET", "HEAD")
            if kind == "interrupt":
                raise KeyboardInterrupt
            if kind == "timeout_before":
                raise self.ddc.ApiUncertain(f"{method} {route}: timed out")
            if kind == "reject":
                raise self.ddc.DdcError(f"HTTP 400 {method} {route}: rejected")
            if kind == "error":
                raise self.ddc.DdcError(f"{method} {route}: connection failed")
            if kind == "notfound":
                if ok404:
                    return None
                raise self.ddc.DdcError(f"HTTP 404 {method} {route}")
            result = self._do(method, route, path, body)
            if kind in ("timeout_after", "5xx_after"):
                raise (self.ddc.ApiUncertain if uncertain else self.ddc.DdcError)(f"{method} {route}: {kind}")
            if kind == "empty":
                return {}
            return result

    def _do(self, method: str, route: str, path: str, body: Any) -> Any:
        if route == "/v1/balances":
            return {"data": {"total": self.balance * 1e6}}
        if (method, route) == ("POST", "/v1/deployments"):
            self._seq += 1
            dseq = str(1000000000100 + self._seq)
            data = body["data"]
            self.deployments[dseq] = {"name": data["name"], "owner": FAKE_OWNER, "state": "active", "t0": self.clock, "limit_h": data["runtimeLimitHours"]}
            return {"data": {"dseq": dseq, "manifest": "manifest-" + dseq}}
        if (method, route) == ("GET", "/v1/deployments"):
            skip = int(dict(p.split("=") for p in path.split("?")[1].split("&")).get("skip", 0)) if "?" in path else 0
            rows = [{"name": v["name"], "deployment": {"id": {"dseq": k, "owner": v["owner"]}}} for k, v in self.deployments.items() if v["state"] == "active"]
            return {"data": {"deployments": rows[skip:skip + 100]}}
        if method == "GET" and route.startswith("/v1/deployments/"):
            dseq = route.rsplit("/", 1)[1]
            if dseq not in self.deployments:
                return None  # unknown dseq: the real API answers 404 (callers pass ok404=True) -> deployment() == {}
            return {"data": self._details(dseq)}
        if method == "DELETE" and route.startswith("/v1/deployments/"):
            dseq = route.rsplit("/", 1)[1]
            if dseq in self.deployments:
                self.deployments[dseq]["state"] = "closed"
                self.deployments[dseq]["closed_t"] = self.clock
            return {}
        if (method, route) == ("GET", "/v1/bids"):
            dseq = path.split("dseq=")[1].split("&")[0]
            return {"data": [{"bid": {"id": {"provider": p, "dseq": dseq, "gseq": 1, "oseq": 1}, "state": "open",
                                      "price": {"denom": "uact", "amount": str(self.bid_uact)}}} for p in self.providers]}
        if (method, route) == ("POST", "/v1/leases"):
            lease = body["leases"][0]
            v = self.deployments[lease["dseq"]]
            v.update(provider=lease["provider"], leased=True, usd_hr=self.bid_uact * 600 / 1e6, t_lease=self.clock)
            if self.next_host is not None:
                v["host"] = self.next_host
            return {"data": {}}
        if (method, route) == ("GET", "/v1/spend-rate"):
            return {"data": {"deployments": [{"dseq": k, "price": {"amount": str(self.bid_uact)}} for k, v in self.deployments.items()
                                             if v["state"] == "active" and v.get("leased")]}}
        raise AssertionError(f"unfaked api call {method} {path}")

    # ------------------------------------------------------------------ ssh / checkpoint verification
    def _box(self, d: dict[str, Any]) -> str:
        return str(d.get("host"))

    def ssh(self, d: dict[str, Any], cmd: str, user: str = "dev", input: bytes | None = None, timeout: int = 600, check: bool = True) -> str:
        host = self._box(d)
        self.ssh_log.append((host, cmd))
        if host in self.unreachable:
            raise self.ddc.DdcError(f"ssh {user}@{host}: `{cmd[:60]}` exit 255: Connection timed out")
        if "checkpoint.sh" in cmd:
            if self.checkpoint == "refuse":
                raise self.ddc.DdcError(f"ssh dev@{host}: `{cmd[:60]}` exit 1: refusing: workspace web-app lost more than half its entries (57 -> 1)")
            if self.checkpoint == "fail":
                raise self.ddc.DdcError(f"ssh dev@{host}: `{cmd[:60]}` exit 1: cannot reach the state repo: Repository not found")
            return f"checkpoint_ok 2026-10-17T10:00:00Z {self._head('commit')} reason=final archives=2\n"
        if "cat /run/ddc/state.json" in cmd:
            return json.dumps({"now": self.clock, **self.agent})
        if "for r in /work/*/" in cmd:
            return "".join(f"{r['name']} {self._head(r['name'])}\n" for r in self.ddc.CFG.get("repos", [])) + \
                "".join(f"{r['name']} -\n" for r in self.ddc.CFG.get("dirs", []))
        if "agent.sh" in cmd and "pgrep" in cmd:
            self.agent_restarts += 1
            return ""
        if "test -f /run/ddc-role" in cmd:
            return "ok\n"
        if "setup.sh" in cmd and "test -f" not in cmd:
            self.restarted.discard(host)
            return f"setup_ok role={cmd.split()[-1]}\n"
        if "test -f /opt/ddc/setup.sh" in cmd:
            return "" if host in self.restarted else "ok\n"
        if "restore.sh" in cmd:
            return "restored= missing=all\n"
        if "ls /work/.ddc/complete" in cmd:
            return " ".join(r["name"] for r in self.ddc.CFG.get("repos", []) + self.ddc.CFG.get("dirs", [])) + "\n"
        return ""

    @staticmethod
    def _head(name: str) -> str:
        return hashlib.sha1(name.encode()).hexdigest()

    def verify_checkpoint(self, expect_commit: str | None = None, keep_local: bool = True) -> dict[str, Any]:
        if self.verify != "ok":
            raise self.ddc.DdcError("checkpoint part ws-web-app.tar.gz.gpg.part000 sha256 mismatch")
        repos = {r["name"]: {"head": self._head(r["name"])} for r in self.ddc.CFG.get("repos", [])}
        names = [r["name"] for r in self.ddc.CFG.get("repos", []) + self.ddc.CFG.get("dirs", []) if r["name"] not in self.drop_archives]
        archives = {f"ws-{n}": {"entries": 10, "parts": []} for n in names} | {"results": {"entries": 1, "parts": []}}
        return {"commit": expect_commit or self._head("commit"), "manifest": {"repos": repos, "archives": archives, "created_at": "2026-10-17T10:00:00Z", "reason": "final"}, "entries": {}}

    # ------------------------------------------------------------------ subprocess (heal, installers)
    def run(self, argv: Any, *a: Any, **k: Any) -> Any:
        argv = list(argv)
        out, rc = b"", 0
        if argv and argv[0] == self.ddc.SSH:
            host = argv[-2].split("@")[-1]
            if host in self.unreachable:
                return subprocess.CompletedProcess(argv, 255, b"", b"ssh: connect to host: Connection timed out")
            out = self.ssh({"host": host}, argv[-1], user="root").encode()
        return subprocess.CompletedProcess(argv, rc, out, b"")
