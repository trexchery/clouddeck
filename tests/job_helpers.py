"""Shared setup for the `ddc job` tests: a virtual-time world (fake Akash API, fake box, fake clock) wired into the controller, a spec factory
and a runner helper.  State, secrets and keys are the per-test isolated ones of tests/conftest.py; nothing here touches the network."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from job_fakes import FakeAkash, FakeRemote, install

from clouddeck.engine import ddc
from clouddeck.job import spec as specmod
from clouddeck.job.clock import FakeClock
from clouddeck.job.runner import JobRunner

SHA = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
RESULT = b"result-bytes" * 1000


class World:
    tmp: Path
    clock: FakeClock
    akash: FakeAkash
    remotes: list[FakeRemote]
    durations: dict[str, float]
    remote_kw: dict[str, Any]
    runner: JobRunner

    def factory(self, host: str, port: int) -> FakeRemote:
        return self.remote({"work": {"out/result.bin": RESULT}})

    def remote(self, produces: dict[str, dict[str, bytes]]) -> FakeRemote:
        r = FakeRemote(self.clock, self.akash, durations=self.durations, produces=produces, **self.remote_kw)
        r.dseq = list(self.akash.deps)[-1]
        self.remotes.append(r)
        return r


def make_world(monkeypatch: Any, tmp_path: Path) -> World:
    w = World()
    w.tmp, w.clock = tmp_path, FakeClock()
    w.akash = FakeAkash(w.clock)
    w.remotes = []
    w.durations = {"cache-m": 120, "work": 600}
    w.remote_kw = {}
    install(monkeypatch, w.clock, w.akash)
    ddc.CFG.update(max_price_usd_hr=5.0, budget_usd=85.0, reserve_usd=5.0)   # the fake GPUs cost ~$2/h: above the default $2/h per-lease ceiling
    return w


def make_spec(tmp: Path, **over: Any) -> dict[str, Any]:
    (tmp / "in.bin").write_bytes(b"input" * 5000)
    raw = {"name": "t1", "runtime_hours": 2, "max_cost_usd": 10, "estimated_runtime_min": 20, "artifacts_dir": "got",
           "hardware": {"cpu": 4, "memory": "16Gi", "disk": "50Gi", "max_usd_hr": 3.0, "gpu": {"vendor": "vendor-a", "models": ["model-a:80Gi"]}},
           "image": {"prebuilt": "ghcr.io/example/base:1.0", "stock": "ubuntu:24.04", "prebuilt_timeout_s": 200},
           "inputs": ["in.bin"], "chunk_mb": 1,
           "cache": {"item": [{"name": "m", "dest": "models/m.bin", "url": "https://example.com/m.bin", "sha256": "a" * 64, "size_gb": 1}]},
           "step": [{"name": "work", "run": "python work.py", "artifacts": ["out/*.bin"]}]}
    raw.update(over)
    return specmod.normalize(raw, tmp)


def run_job(w: World, spec: dict[str, Any], **kw: Any) -> JobRunner:
    kw.setdefault("remote_factory", w.factory)
    kw.setdefault("install_signals", False)
    kw.setdefault("log", lambda m: None)
    r = JobRunner(spec, clock=w.clock, **kw)
    w.runner = r
    r.run()
    return r
