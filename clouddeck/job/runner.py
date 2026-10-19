"""Job orchestration: bid early / lease late, overlap prep with boot, stream-start steps, verified retrieval, verified close.

Single-threaded and cooperative: every wait loop calls `pump()` (local prep status, one upload chunk, launching steps),
so there is no locking and the whole flow runs deterministically on a fake clock in tests and simulations.
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shlex
import shutil
import signal
import string
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clouddeck import controller as ddc

from . import preflight
from .clock import RealClock
from .market import History, Market, ProvisionFailed
from .remote import HostKeyChanged, SshRemote
from .spec import URL_RE, image_kinds, render_sdl, safe_rel_path, scrub
from .telemetry import Telemetry, format_summary

MIN_FREE_DISK = 256 << 20   # artifact retrieval keeps this much of the local disk free on top of what it pulls
HEARTBEAT_S = 60   # how often the runner records "I am alive" on its lease record (the watchdog closes a job lease that stays silent: ddc.reap_jobs)


class JobError(RuntimeError):
    pass


class StepFailed(JobError):
    pass


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class SubprocessPrep:
    """Local preparation command running while the lease is being bid/booted."""

    def __init__(self, cmd: str, cwd: str):
        self.cmd, self.cwd, self.proc, self.rc = cmd, cwd, None, None

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, shell=True, cwd=self.cwd)

    def done(self) -> bool:
        if self.rc is None and self.proc is not None and self.proc.poll() is not None:
            self.rc = self.proc.returncode
        return self.rc is not None

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


class JobRunner:
    def __init__(self, spec: dict[str, Any], *, clock=None, remote_factory: Callable[[str, int], Any] | None = None,
                 prep_factory: Callable[[dict[str, Any]], Any] | None = None, overlap: bool = True, keep: bool = False,
                 prefer_prebuilt: bool = True, offline: bool = False, log: Callable[[str], None] | None = None,
                 state_dir: Path | None = None, install_signals: bool = True):
        self.s, self.clock = spec, clock or RealClock()
        self.log = log or ddc.log
        for note in preflight.apply_limits(self.s):   # the spec's own caps never exceed the controller's configuration
            self.log(f"limit: {note}")
        self.overlap, self.keep, self.prefer_prebuilt, self.offline = overlap, keep, prefer_prebuilt, offline
        self.state_dir = state_dir or ddc.STATE_DIR / "jobs"
        self.base = Path(spec["base"])
        self.art_dir = self.base / spec["artifacts_dir"]
        self.tel = Telemetry(spec["name"], self.clock, ddc.STATE_DIR / "telemetry" / f"{spec['name']}-{int(self.clock.time())}.json")
        self.hist = History(ddc.STATE_DIR / "provider_history.json", self.clock)
        self.remote_factory = remote_factory or self._ssh_remote
        self.prep_factory = prep_factory or (lambda s: SubprocessPrep(s["prep"]["cmd"], s["base"]) if s["prep"]["cmd"] else None)
        self.install_signals = install_signals
        self.market: Market | None = None
        self.remote: Any = None
        self.prep: Any = None
        self.prep_started = 0.0
        self.dseq: str | None = None
        self.usd_hr = float(spec["hardware"]["max_usd_hr"] or 0)
        self.provider = ""
        self.image = ""
        self.closed = False
        self.cost: float | None = None
        self.err: BaseException | None = None
        self.hard_stop = False
        self.interrupts = 0
        self._closing = False           # True while the lease is being settled and closed: signals are deferred, not raised
        self._deferred_signal: int | None = None
        self._last_beat = float("-inf")
        self.rec: dict[str, Any] = {"uploaded": {}, "retrieved": {}}
        self.launched = False
        self.inputs_done = False
        self.setup_logged = False
        self.retrieved_steps: set[str] = set()
        self.inputs: list[dict[str, Any]] = []
        self._deadline: float | None = None
        self.artifacts: list[dict[str, Any]] = []

    # ------------------------------------------------------------ public
    def run(self) -> dict[str, Any]:
        lock = ddc.FileLock(f"jobs/{self.s['name']}.lock", wait_s=0)   # an OS lock: held for the whole run, released by the OS if this process dies (a crash-resume still works)
        try:
            lock.__enter__()
        except ddc.LockBusy:
            raise JobError(f"another `ddc job run` of {self.s['name']!r} is running (it holds {lock.p}). Wait for it, or stop it, or use another job name") from None
        try:
            return self._run_locked()
        finally:
            lock.__exit__(None, None, None)

    def _run_locked(self) -> dict[str, Any]:
        old = self._install_signals()
        atexit.register(self._atexit)
        try:
            self._run()
        except BaseException as e:  # noqa: BLE001  (KeyboardInterrupt/SystemExit included: we must still close)
            self.err = e
            raise
        finally:
            try:
                self._finish()
            finally:
                if self.closed or not self.dseq or (self.keep and self.err is None):   # not before the lease is closed: if that failed, the exit hook tries once more
                    atexit.unregister(self._atexit)
                self._restore_signals(old)
        if self._deferred_signal is not None:   # a signal that arrived during the close: the close finished first, now it is honoured
            raise KeyboardInterrupt
        return self.tel.to_dict(self.usd_hr)

    # ------------------------------------------------------------ signals / cleanup
    def _install_signals(self) -> dict[int, Any]:
        old: dict[int, Any] = {}
        if not self.install_signals or threading.current_thread() is not threading.main_thread():
            return old

        def handler(signum, frame):
            self.interrupts += 1
            if self.interrupts > 1:
                self.hard_stop = True  # second Ctrl-C: skip artifact salvage, just close the lease
            if self._closing:   # the verified close must not be aborted: remember it, finish closing, raise afterwards
                self._deferred_signal = signum
                return
            raise KeyboardInterrupt

        for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):   # SIGHUP: the terminal or the ssh session that runs this went away (POSIX)
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    old[sig] = signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
        return old

    def _restore_signals(self, old: dict[int, Any]) -> None:
        for sig, h in old.items():
            try:
                signal.signal(sig, h)
            except (ValueError, OSError):
                pass

    def _atexit(self) -> None:  # safety net if the process dies on a path that skipped `finally`, or if the close in `finally` failed
        self._closing = True
        try:
            self._settle_unknown()
            if self.dseq and not self.closed and not (self.keep and self.err is None):
                try:
                    self._close()
                except BaseException as e:  # noqa: BLE001
                    self.log(f"ATEXIT close failed: {e}. platform runtimeLimitHours is the backstop")
        finally:
            self._closing = False

    def _settle_unknown(self) -> None:
        """The runner holds a creation it never got a dseq for (an interrupt or an error between the POST and the return of launch_lease): find
        what it created by its unique name.  A deployment already recorded under that name becomes self.dseq (and is closed like any lease); a
        write-ahead `pending` record is resolved by the controller's reconcile (adopt by name, close), under the same lock as every creation."""
        m = self.market
        if self.dseq or not m or not m.name:
            return
        try:
            for dseq, rec in ddc.load_state()["deps"].items():
                if rec.get("name") == m.name and rec.get("status") != "closed":
                    self.dseq = m.dseq = str(dseq)
                    self.log(f"the creation of {m.name} finished as dseq {dseq} while the runner was interrupted: closing it")
                    return
            if m.name in ddc.pending_creations():
                self.log(f"{m.name}: creation outcome unknown (interrupted before the reply): looking it up by name and closing it")
                with ddc.FileLock(wait_s=120):
                    for msg in ddc.reconcile_pending():
                        self.log(msg)
        except Exception as e:  # noqa: BLE001
            self.log(f"CREATION NOT RESOLVED for {m.name}: {e}. Run `ddc reconcile` (the watchdog also does): it may be billing")

    def _finish(self) -> None:
        try:
            if self.err is not None:
                self.log(f"job failed: {type(self.err).__name__}: {self.err}")
                if self.prep is not None and hasattr(self.prep, "stop"):
                    self.prep.stop()
                if self.remote is not None and self.tel.marks.get("ssh_ready") and not self.hard_stop:
                    self._salvage()
        finally:  # whatever happened above (even a second Ctrl-C), the lease is closed next, and no signal may abort that
            self._closing = True
            try:
                self._settle_unknown()
                self._close_and_report()
            finally:
                self._closing = False

    def _close_and_report(self) -> None:
        deferred: BaseException | None = None
        if self.dseq and not (self.keep and self.err is None):
            try:
                self._close()
            except Exception as e:  # noqa: BLE001
                self.log(f"CLOSE NOT VERIFIED for dseq {self.dseq}: {e}. Run `ddc status` / `ddc job close {self.s['name']}`; "
                         f"platform runtimeLimitHours={self.market.limit_h if self.market else '?'} h is the backstop")
            except BaseException as e:  # noqa: BLE001  (the lease IS closed: an interrupt that landed in the close is honoured after the report)
                deferred = e
        elif self.dseq:
            self.log(f"--keep: lease {self.dseq} left running (${self.usd_hr:.3f}/h). Close it: ddc job close {self.s['name']}. Nothing heartbeats it any more, so the "
                     f"watchdog closes it after job_orphan_minutes ({ddc.CFG.get('job_orphan_minutes', ddc.JOB_ORPHAN_MINUTES)} min by default) at the latest")
        self.tel.extra["artifacts"] = self.artifacts
        self.tel.extra["image"] = self.image
        self.tel.extra["cost_usd"] = self.cost
        self.tel.extra["ok"] = self.err is None
        self.tel.write(self.usd_hr)
        self.log("\n" + format_summary(self.tel.to_dict(self.usd_hr)))
        if deferred is not None:
            raise deferred

    def _salvage(self) -> None:
        """Best effort: pull artifacts of steps that already finished, bounded by salvage_s."""
        self._deadline = self.clock.time() + self.s["salvage_s"]
        try:
            m = self.remote.markers()
            for st in self.s["steps"]:
                if st["artifacts"] and m.get(st["name"], {}).get("state") == "done" and st["name"] not in self.retrieved_steps:
                    self._retrieve(st)
        except BaseException as e:  # noqa: BLE001  (a second Ctrl-C lands here: stop salvaging, go close)
            self.log(f"salvage stopped: {type(e).__name__}: {e}")
        finally:
            self._deadline = None

    def _close(self) -> None:
        if self.closed or not self.dseq:
            return
        last: BaseException | None = None
        deferred: BaseException | None = None
        failures = interrupts = 0
        while failures < 3 and interrupts < 10:
            try:
                self.cost = ddc.close_box(self.dseq)  # DELETE, wait for state=closed, assert absent from /v1/spend-rate
                self.closed = True
                self.tel.mark("closed")
                self._save_record(status="closed")
                self._forget_host_keys(self.dseq)
                break
            except ddc.DdcError as e:
                last = e
                failures += 1
                self.clock.sleep(5)
            except BaseException as e:  # noqa: BLE001  (Ctrl-C / SIGTERM / SystemExit in the middle of the close: it must not leave the lease open)
                deferred = deferred or e
                last = e
                interrupts += 1
                self.log(f"{type(e).__name__} during the close of dseq {self.dseq}: closing first, then it is honoured")
        if not self.closed:
            raise JobError(str(last))
        if deferred is not None:
            raise deferred

    # ------------------------------------------------------------ main flow
    def _run(self) -> None:
        checks = preflight.run(self.s, offline=self.offline, clock=self.clock)
        for c in checks:
            self.log(f"preflight {c.status.upper():4} {c.name}: {c.detail}")
        bad = [c for c in checks if c.status == "fail"]
        if bad:
            raise JobError("preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in bad))
        rec = self._load_record()
        if rec and self._resumable(rec):
            self._resume(rec)
        else:
            if rec:
                self._close_orphan(rec)
            if self.overlap:
                self._start_prep()
            self._provision()
        self._monitor()

    def _start_prep(self) -> None:
        self.prep = self.prep_factory(self.s)
        if self.prep:
            self.prep_started = self.clock.time()
            self.tel.mark("prep_start")
            self.prep.start()
            self.log(f"local prep started: {self.s['prep']['cmd']}")

    # ---- provisioning
    def _provision(self) -> None:
        attempts = image_kinds(self.s, self.prefer_prebuilt)
        i, retried = 0, False
        while True:
            kind, image = attempts[i]
            try:
                self._provision_one(kind, image)
                return
            except ProvisionFailed as e:
                self.log(f"provision with {kind} image failed: {e}")
                if e.image_related and self.market and self.market.dseq and self.provider:
                    self.hist.record(self.provider, image, ok=False)
                self._close()
                if (e.image_related and i == len(attempts) - 1) or (not e.image_related and retried):
                    raise JobError(f"could not provision a box: {e}") from e
                if not e.image_related:
                    retried = True
                    self.log("retrying the same image once with fresh bids")
                else:
                    i += 1
                    self.log("falling back to the stock image (apt bootstrap)")
                self._reset_attempt(kind)

    def _reset_attempt(self, kind: str) -> None:
        """Keep the failed attempt's cost in the telemetry, then clear the per-attempt marks for the next one."""
        m = self.tel.marks
        wasted = {"image_kind": kind, "billed_s": round((m.get("closed", m["lease"] if "lease" in m else 0) - m["lease"]), 1) if "lease" in m else 0.0,
                  "usd_hr": self.usd_hr}
        self.tel.extra.setdefault("failed_attempts", []).append(wasted)
        self.tel.mark("image_fallback", force=True)
        for k in ("create", "bid", "lease", "manifest", "container_ready", "ssh_ready", "container_start", "closed"):
            m.pop(k, None)
        self.closed, self.dseq, self.provider, self.remote = False, None, "", None

    def _provision_one(self, kind: str, image: str) -> None:
        s = self.s
        self.image = image
        sdl = render_sdl(s, image, ddc.pubkey("access"))
        m = self.market = Market(s, self.clock, self.tel, self.hist, self.log)
        self.dseq = m.create(sdl, image)
        self._save_record(status="created", image=image)
        choice = self._wait_for_bid(m)
        m.lease(choice)
        self.usd_hr, self.provider = choice["usd_hr"], choice["provider"]
        self._save_record(status="leased", provider=self.provider, usd_hr=self.usd_hr)
        host, port = m.endpoint(self.s["image"]["prebuilt_timeout_s" if kind == "prebuilt" else "stock_timeout_s"], beat=self._beat)
        ddc.update_dep(self.dseq, host=host, port=port)
        self.remote = self.remote_factory(host, port)
        self._wait_ssh(kind, image)
        ddc.update_dep(self.dseq, status="ready")   # not "leased" any more: the watchdog's never-became-ready rule must not close a lease that is working
        self._save_record(status="ready", host=host, port=port)

    def _wait_for_bid(self, m: Market) -> dict[str, Any]:
        """Bid early, lease late: bids are collected immediately, the lease waits until it can overlap the remaining prep."""
        t0 = t_seen = self.clock.time()
        while True:
            self.pump()
            ranked = m.bids()
            waited = self.clock.time() - t0
            if ranked:
                t_seen = self.clock.time()
            if ranked and (len(ranked) >= 3 or waited >= self.s["bid_wait_s"]):
                if self._lease_now(ranked[0]):
                    self.log(f"{len(ranked)} acceptable bid(s); chose {ranked[0]['host'] or ranked[0]['provider']} ${ranked[0]['usd_hr']:.3f}/h "
                             f"(expected boot {ranked[0]['boot_s']:.0f}s)")
                    return ranked[0]
                if waited > self.s["max_prep_wait_s"]:
                    raise JobError("local prep did not finish within max_prep_wait_s; nothing leased")
            elif self.clock.time() - t_seen > 240:
                raise ProvisionFailed("no acceptable bids", image_related=False)
            self.clock.sleep(10 if ranked else 4)  # once bids exist only the lease gate is waiting: poll gently

    def _lease_now(self, choice: dict[str, Any]) -> bool:
        if not self.overlap or self.prep is None or self.prep.done():
            return True
        est = self.s["prep"]["estimate_s"]
        if not est:
            return False  # no ETA: lease only once prep is done (never pay while waiting on an unknown)
        remaining = self.prep_started + est - self.clock.time()
        return remaining <= choice["boot_s"] + self.s["setup_estimate_s"]

    def _wait_ssh(self, kind: str, image: str) -> None:
        timeout = self.s["image"]["prebuilt_timeout_s" if kind == "prebuilt" else "stock_timeout_s"]
        t_end = self.tel.marks["lease"] + timeout
        while True:
            try:
                if self.remote.probe():
                    break
            except HostKeyChanged as e:   # not "sshd not up yet": waiting longer cannot fix it and the lease bills meanwhile
                raise ProvisionFailed(str(e), image_related=False) from e
            if self.clock.time() >= t_end:
                raise ProvisionFailed(f"sshd not up {timeout:.0f}s after lease")
            self.pump()
            self.clock.sleep(6)
        ssh = self.tel.mark("ssh_ready")
        info = self.remote.boot_info()
        if info:
            self.tel.mark("container_start", at=info["start"])
            self.tel.extra["boot"] = {"container_start_after_lease_s": round(info["start"] - self.tel.marks["lease"], 1),
                                      "toolchain_s": round(info["toolchain_done"] - info["start"], 1),
                                      "apt_installed": info.get("installed", ""), "image_kind": kind}
        self.hist.record(self.provider, image, ok=True, boot_s=ssh - self.tel.marks["lease"])
        self.remote.install_scripts()
        self.remote.push_secrets({k: os.environ[k] for k in self.s["secrets"] if k in os.environ})
        self.remote.mark_done("toolchain")
        self.log(f"ssh ready {ssh - self.tel.marks['lease']:.0f}s after lease" + (f" (apt installed: {scrub(info.get('installed') or 'nothing', 200)})" if info else ""))

    # ---- resume
    def _rec_path(self) -> Path:
        return self.state_dir / f"{self.s['name']}.json"

    def _load_record(self) -> dict[str, Any] | None:
        return ddc.read_json(self._rec_path(), None)

    def _save_record(self, **kw: Any) -> None:
        self.rec |= {"name": self.s["name"], "dseq": self.dseq, "marks": self.tel.marks, "image": self.image, **kw}
        ddc.write_json(self._rec_path(), self.rec)

    def _resumable(self, rec: dict[str, Any]) -> bool:
        if rec.get("status") in (None, "closed", "created", "leased") or not rec.get("dseq") or not rec.get("host"):
            return False
        if str(rec["dseq"]) not in ddc.job_leases():  # the record must point at an active job lease of ours, never at anything else
            return False
        det = ddc.deployment(str(rec["dseq"]))
        return bool(det) and (det.get("deployment") or {}).get("state") != "closed"

    def _close_orphan(self, rec: dict[str, Any]) -> None:
        """A previous run died after creating a lease that cannot be resumed: close it so it stops billing."""
        if rec.get("status") == "closed" or not rec.get("dseq"):
            return
        dseq = str(rec["dseq"])
        if dseq not in ddc.job_leases():  # same rule as _resumable: the record may only ever point at an active job lease of ours
            self.log(f"ignoring the record of an earlier run: dseq {dseq} is not an active job lease of ours")
        else:
            det = ddc.deployment(dseq)
            if det and (det.get("deployment") or {}).get("state") != "closed":
                self.log(f"closing orphaned lease {dseq} from an earlier run of this job")
                ddc.close_box(dseq)
            self._forget_host_keys(dseq)
        self.rec = {"uploaded": {}, "retrieved": {}}

    def _resume(self, rec: dict[str, Any]) -> None:
        self.log(f"resuming job {self.s['name']} on open lease {rec['dseq']} (steps and transfers are idempotent)")
        self.rec = rec
        self.dseq, self.provider, self.image = str(rec["dseq"]), rec.get("provider", ""), rec.get("image", "")
        self.usd_hr = float(rec.get("usd_hr") or self.usd_hr)
        self.tel.marks.update(rec.get("marks") or {})
        self.market = Market(self.s, self.clock, self.tel, self.hist, self.log)
        self.market.dseq = self.dseq
        self._beat(force=True)
        self.remote = self.remote_factory(rec["host"], int(rec["port"]))
        try:
            reachable = self.remote.probe()
        except HostKeyChanged as e:
            raise JobError(f"resume: {e} (the container was replaced); closing the lease") from e
        if not reachable:
            raise JobError("resume: box unreachable (container restarted or provider down)")
        self.remote.boot_info()
        self.remote.install_scripts()
        if self.overlap and not self.prep:
            self._start_prep()

    # ------------------------------------------------------------ pump: local prep, uploads, step launch
    def _beat(self, force: bool = False) -> None:
        """Throttled heartbeat on the lease record: if this process dies, the watchdog sees it go stale and closes the lease."""
        t = self.clock.time()
        if not self.dseq or (not force and t - self._last_beat < HEARTBEAT_S):
            return
        self._last_beat = t
        try:
            ddc.update_dep(self.dseq, heartbeat_at=t)
        except Exception as e:  # noqa: BLE001  (a missed beat only makes the watchdog's orphan rule stricter)
            self.log(f"warning: cannot record the job heartbeat: {e}")

    def pump(self) -> bool:
        """Advance local work without blocking on the box. Returns True if it did something (caller then skips its sleep)."""
        self._beat()
        if self.prep is not None and self.prep.done() and "prep_done" not in self.tel.marks:
            self.tel.mark("prep_done")
            if getattr(self.prep, "rc", 0):
                raise JobError(f"local prep failed (rc={self.prep.rc}); closing without running anything")
        if self.remote is None or "ssh_ready" not in self.tel.marks:
            return False
        if not self.overlap and self.prep is None and self.s["prep"]["cmd"] and "prep_start" not in self.tel.marks:
            self._start_prep()  # legacy ordering: prep only starts once the box is up
        busy = self._upload_tick()
        if not self.launched and (self.overlap or (self._prep_finished() and self.inputs_done)):
            self._launch()
            busy = True
        return busy

    def _prep_finished(self) -> bool:
        return self.prep is None or self.prep.done()

    def _input_list(self) -> list[dict[str, Any]]:
        if not self.inputs:
            for p in self.s["inputs"]:
                lp = self.base / p
                self.inputs.append({"local": lp, "remote": f"{self.s['inputs_dest']}/{lp.name}", "sha": None, "off": None, "done": False,
                                    "produced": p in self.s["prep"]["produces"] or bool(self.s["prep"]["cmd"] and not lp.exists())})
        return self.inputs

    def _upload_tick(self) -> bool:
        if self.inputs_done:
            return False
        rm, items = self.remote, self._input_list()
        for it in items:
            if it["done"]:
                continue
            if it["produced"] and not self._prep_finished():
                continue
            if not it["local"].exists():
                if self._prep_finished():
                    raise JobError(f"input {it['local']} missing after prep")
                continue
            size = it["local"].stat().st_size
            if it["sha"] is None:
                it["sha"] = sha256_file(it["local"])
            have, final = rm.size(it["remote"])
            if final and have == size and self.rec["uploaded"].get(it["remote"]) == it["sha"]:
                it["done"] = True
                return True
            if final:  # stale or untrusted final file: re-upload over it
                have = 0
                rm.drop_part(it["remote"])
            n = self.s["chunk_mb"] << 20
            if have < size or size == 0:
                with open(it["local"], "rb") as f:
                    f.seek(have)
                    data = f.read(n)
                if data and not rm.put(it["remote"], have, data):
                    return True  # offset moved under us: next tick re-reads the remote size
                have += len(data)
            if have >= size:
                if not rm.finalize(it["remote"], it["sha"]):
                    rm.drop_part(it["remote"])
                    it["fails"] = it.get("fails", 0) + 1
                    if it["fails"] >= 3:
                        raise JobError(f"upload of {it['local'].name} failed sha256 verification 3 times")
                    return True
                it["done"] = True
                self.rec["uploaded"][it["remote"]] = it["sha"]
                self._save_record()
            return True
        if all(i["done"] for i in items):
            if self._prep_finished():
                self.inputs_done = True
                rm.mark_done("inputs")
                self.tel.mark("inputs_ready")
                return True
        return False

    def _urls(self, item: dict[str, Any]) -> list[str]:
        mirrors = [m.rstrip("/") + "/" + item["path"] for m in self.s["cache"]["mirrors"]]
        known = {**self.s["env"], **{k: os.environ[k] for k in self.s["secrets"] if k in os.environ}}   # only what the spec declares: never the whole local environment
        urls = [string.Template(u).safe_substitute(known) for u in mirrors + item["urls"]]
        if not all(URL_RE.fullmatch(u) for u in urls):   # re-checked after the substitution: a variable must not turn a source into an option, a newline or another scheme
            raise JobError(f"download item {item['name']}: a source is not a plain http(s) URL after variable substitution")
        return urls

    def _launch(self) -> None:
        rm = self.remote
        for it in self.s["cache"]["items"]:
            if not it["sha256"] and not it.get("allow_unverified"):
                raise JobError(f"download item {it['name']} has no sha256 and is not marked allow_unverified")
            args = " ".join(shlex.quote(a) for a in [it["dest"], it["sha256"] or "-", *self._urls(it)])
            rm.start(f"cache-{it['name']}", [], f"bash .ddc/fetch.sh {args}")
        for st in self.s["steps"]:
            rm.start(st["name"], st["needs"], st["run"])
        self.launched = True
        self.log(f"launched {len(self.s['cache']['items'])} download(s) and {len(self.s['steps'])} step(s); steps start as their needs complete")
        if not self.s["inputs"] and not self.s["prep"]["produces"]:
            rm.mark_done("inputs")
            self.inputs_done = True

    # ------------------------------------------------------------ monitor / retrieve
    def _monitor(self) -> None:
        s = self.s
        last_act, next_poll = self.clock.time(), 0.0
        while True:
            busy = self.pump()
            now = self.clock.time()
            if busy:
                last_act = now
            if now >= next_poll:
                m = self.remote.markers()
                next_poll = self.clock.time() + s["poll_s"]
                if self._observe(m):
                    last_act = self.clock.time()
                if self.overlap or self._all_done(m):
                    self._retrieve_ready(m)
                if self._all_done(m) and len(self.retrieved_steps) >= sum(1 for st in s["steps"] if st["artifacts"]):
                    if not any(st["artifacts"] for st in s["steps"]):
                        self.tel.mark("artifact_retrieved", at=self.tel.marks.get("job_done"))
                    return
            now = self.clock.time()
            self._guard()
            if now - last_act >= s["idle_abort_s"]:
                raise JobError(f"idle for {s['idle_abort_s']:.0f}s with nothing running or transferring; aborting to stop paid idle time")
            if not busy:
                self.clock.sleep(max(0.5, min(s["poll_s"], next_poll - self.clock.time())))

    def _guard(self) -> None:
        """The job's own cost and wall-clock limits.  Called by the monitor loop AND inside every long transfer, so a retrieval cannot run past them."""
        s, now = self.s, self.clock.time()
        lease_t = self.tel.marks.get("lease")
        if lease_t is None:
            return
        if (now - lease_t) / 3600 * self.usd_hr >= s["max_cost_usd"]:
            raise JobError(f"budget guard: estimated cost reached ${s['max_cost_usd']:.2f}")
        if now - lease_t >= max(600.0, s["runtime_hours"] * 3600 - 300):
            raise JobError("wall-clock limit reached (just under runtimeLimitHours)")

    def _all_done(self, m: dict[str, dict[str, Any]]) -> bool:
        names = [st["name"] for st in self.s["steps"]]
        return all(m.get(n, {}).get("state") == "done" for n in names)

    def _observe(self, m: dict[str, dict[str, Any]]) -> bool:
        s = self.s
        for n, i in m.items():
            if i.get("state") == "failed":
                raise StepFailed(f"step {scrub(n, 60)} failed (rc={scrub(i.get('rc'), 20)}{', ' + scrub(i['why'], 200) if i.get('why') else ''}); log tail:\n{scrub(self.remote.tail_log(n))}")
        prod = {st["name"]: st["productive"] for st in s["steps"]}
        for n, productive in [*prod.items(), *((f"cache-{c['name']}", False) for c in s["cache"]["items"])]:
            if n in m and m[n].get("start"):
                self.tel.step(n, m[n].get("start"), m[n].get("end"), productive, m[n].get("rc"))
        started = [m[n]["start"] for n, p in prod.items() if p and m.get(n, {}).get("start")]
        if started:
            self.tel.mark("first_job_start", at=min(started))
        setup = [c for c in ([f"cache-{x['name']}" for x in s["cache"]["items"]] + [n for n, p in prod.items() if not p])]
        if "setup_done" not in self.tel.marks and self.inputs_done and all(m.get(n, {}).get("state") == "done" for n in setup):
            self.tel.mark("setup_done", at=max([m[n]["end"] for n in setup] + [self.tel.marks.get("inputs_ready", 0)]))
        prod_names = [n for n, p in prod.items() if p]
        if "job_done" not in self.tel.marks and prod_names and all(m.get(n, {}).get("state") == "done" for n in prod_names):
            self.tel.mark("job_done", at=max(m[n]["end"] for n in prod_names))
        return any(i.get("state") == "running" for i in m.values())

    def _retrieve_ready(self, m: dict[str, dict[str, Any]]) -> None:
        for st in self.s["steps"]:
            if st["artifacts"] and st["name"] not in self.retrieved_steps and m.get(st["name"], {}).get("state") == "done":
                self._retrieve(st)
        if all(st["name"] in self.retrieved_steps for st in self.s["steps"] if st["artifacts"]) and any(st["artifacts"] for st in self.s["steps"]):
            self.tel.mark("artifact_retrieved")

    def _retrieve(self, st: dict[str, Any]) -> None:
        entries = self.remote.glob(st["artifacts"])
        if not entries:
            raise JobError(f"step {st['name']} finished but no file matches {st['artifacts']}")
        cap = int(self.s["max_artifact_gb"] * (1 << 30))   # the sizes are reported by the (untrusted) box: bound what the runner is willing to pull
        have = sum(a["size"] for a in self.artifacts if a["path"] not in {e[0] for e in entries})
        if any(size > cap for _, size, _ in entries) or have + sum(size for _, size, _ in entries) > cap:
            raise JobError(f"step {st['name']}: the box reports {sum(e[1] for e in entries) / (1 << 30):.2f} GiB of artifacts, above max_artifact_gb = {self.s['max_artifact_gb']:g}; not pulling them")
        for path, size, sha in entries:
            self._pull(path, size, sha)
        self.retrieved_steps.add(st["name"])

    def _pull(self, path: str, size: int, sha: str) -> None:
        dst = self.art_dir / path
        root = self.art_dir.resolve()
        if not safe_rel_path(path) or root not in dst.resolve().parents:  # the path was reported by the (untrusted) box: it must land below artifacts_dir
            raise JobError(f"refusing artifact path {path[:80]!r}: it does not stay below {self.art_dir}")
        if size > int(self.s["max_artifact_gb"] * (1 << 30)):
            raise JobError(f"artifact {scrub(path, 80)} is reported as {size / (1 << 30):.2f} GiB, above max_artifact_gb = {self.s['max_artifact_gb']:g}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size == size and sha256_file(dst) == sha:
            self._record_artifact(path, size, sha, dst)
            return
        part = dst.with_name(dst.name + ".part")
        n = self.s["chunk_mb"] << 20
        for _attempt in range(3):
            have = part.stat().st_size if part.exists() else 0
            if have > size:
                part.unlink()
                have = 0
            free = shutil.disk_usage(dst.parent).free
            if free < size - have + MIN_FREE_DISK:
                raise JobError(f"not enough free disk for {scrub(path, 80)}: {free / (1 << 30):.2f} GiB free here, {(size - have) / (1 << 30):.2f} GiB to pull")
            while have < size:
                if self._deadline is not None and self.clock.time() > self._deadline:
                    raise JobError("salvage time budget spent")
                self._guard()   # the job's budget and wall-clock limits hold during retrieval too
                self._beat()
                data = self.remote.get(path, have, n)
                if not data:
                    raise JobError(f"remote returned no data for {scrub(path, 80)} at offset {have}")
                data = data[: size - have]   # never write more than the size the box announced
                with open(part, "ab") as f:
                    f.write(data)
                have += len(data)
            if sha256_file(part) == sha:
                os.replace(part, dst)
                self._record_artifact(path, size, sha, dst)
                return
            part.unlink(missing_ok=True)  # corrupt resume: restart this file from zero
        raise JobError(f"artifact {path} failed sha256 verification 3 times")

    def _record_artifact(self, path: str, size: int, sha: str, dst: Path) -> None:
        self.rec["retrieved"][path] = sha
        if not any(a["path"] == path for a in self.artifacts):
            self.artifacts.append({"path": path, "local": str(dst), "size": size, "sha256": sha})
        self.log(f"artifact {scrub(path, 200)} verified sha256={sha[:12]}")
        self._save_record()

    # ------------------------------------------------------------ remote factory
    def _ssh_remote(self, host: str, port: int) -> SshRemote:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return SshRemote(host, port, ddc.KEYS / "access", self._known_hosts(self.dseq), self.clock, beat=self._beat)

    def _known_hosts(self, dseq: str | None) -> Path:
        """One host-key file per LEASE: a provider that reuses host:port for a new container hands out a new host key, which a file kept per job (or
        per box address) would refuse for ever.  A resume of the same dseq finds its own file again."""
        return self.state_dir / f"{self.s['name']}-{dseq}.known_hosts"

    def _forget_host_keys(self, dseq: str | None) -> None:
        try:
            self._known_hosts(dseq).unlink(missing_ok=True)
        except OSError:
            pass
