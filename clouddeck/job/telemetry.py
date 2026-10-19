"""Per-phase startup/shutdown telemetry: JSON file + printed summary of billed vs productive time."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PHASES = ["create", "bid", "lease", "manifest", "container_ready", "ssh_ready", "setup_done", "first_job_start",
          "job_done", "artifact_retrieved", "closed"]


def union_len(iv: list[tuple[float, float]]) -> float:
    tot, end = 0.0, None
    start = None
    for a, b in sorted(iv):
        if end is None or a > end:
            if end is not None:
                tot += end - start
            start, end = a, b
        else:
            end = max(end, b)
    return tot + (end - start if end is not None else 0.0)


def breakdown(marks: dict[str, float], steps: dict[str, dict[str, Any]], usd_hr: float, now: float | None = None) -> dict[str, Any]:
    """Billing runs from `lease` to `closed` (or `now` while open). Productive = union of productive step intervals."""
    lease, closed = marks.get("lease"), marks.get("closed")
    out: dict[str, Any] = {"usd_hr": usd_hr}
    if lease is None:
        return out | {"billed_s": 0.0, "productive_s": 0.0}
    end = closed if closed is not None else (now if now is not None else lease)
    billed = max(0.0, end - lease)
    iv = [(v["start"], v["end"]) for v in steps.values() if v.get("productive") and v.get("start") and v.get("end")]
    busy = union_len(iv)
    ssh, fj, jd = marks.get("ssh_ready"), marks.get("first_job_start"), marks.get("job_done")
    parts = {"boot_s": (ssh - lease) if ssh else None,
             "wait_before_first_job_s": (fj - ssh) if fj and ssh else None,
             "gaps_between_jobs_s": ((jd - fj) - busy) if jd and fj else None,
             "after_last_job_s": (end - jd) if jd else None}
    out |= {"billed_s": billed, "productive_s": busy, "idle_overhead_s": max(0.0, billed - busy), "parts": parts,
            "productive_pct": round(100 * busy / billed, 1) if billed else 0.0, "billed_usd_est": round(billed / 3600 * usd_hr, 4),
            "idle_usd_est": round(max(0.0, billed - busy) / 3600 * usd_hr, 4)}
    pre = marks.get("create")
    if pre is not None:
        out["create_to_lease_s"] = lease - pre
    return out


class Telemetry:
    def __init__(self, job: str, clock, path: Path | None = None):
        self.job, self.clock, self.path = job, clock, path
        self.marks: dict[str, float] = {}
        self.steps: dict[str, dict[str, Any]] = {}
        self.extra: dict[str, Any] = {}

    def mark(self, name: str, at: float | None = None, force: bool = False) -> float:
        if force or name not in self.marks:
            self.marks[name] = self.clock.time() if at is None else at
        return self.marks[name]

    def step(self, name: str, start: float | None, end: float | None, productive: bool, rc: int | None = None) -> None:
        self.steps[name] = {"start": start, "end": end, "productive": productive, "rc": rc}

    def to_dict(self, usd_hr: float = 0.0) -> dict[str, Any]:
        m = {k: self.marks[k] for k in PHASES if k in self.marks} | {k: v for k, v in self.marks.items() if k not in PHASES}
        t0 = m.get("create")
        rel = {k: round(v - t0, 1) for k, v in m.items()} if t0 is not None else {}
        return {"job": self.job, "marks_epoch": m, "marks_rel_create_s": rel, "steps": self.steps,
                "summary": breakdown(self.marks, self.steps, usd_hr, self.clock.time()), **self.extra}

    def write(self, usd_hr: float = 0.0) -> Path | None:
        if not self.path:
            return None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_dict(usd_hr), indent=1), encoding="utf-8")
        return self.path

    def summary(self, usd_hr: float = 0.0) -> str:
        return format_summary(self.to_dict(usd_hr))


def format_summary(d: dict[str, Any]) -> str:
    m, sm = d["marks_epoch"], d["summary"]
    t0 = m["create"] if "create" in m else min(m.values(), default=0.0)   # an interrupted creation has no `create` mark: count from the first one there is
    lines = [f"== {d['job']}: startup/shutdown telemetry =="]
    prev = None
    for k in PHASES:
        if k in m:
            lines.append(f"  {k:<19} +{m[k] - t0:7.1f}s" + (f"  (+{m[k] - prev:.1f}s)" if prev is not None else ""))
            prev = m[k]
    if "billed_s" in sm and sm["billed_s"]:
        p = sm.get("parts") or {}

        def f(v):
            return "-" if v is None else f"{v:.0f}s"
        lines += [f"  billed (lease->closed) {sm['billed_s']:.0f}s = ${sm['billed_usd_est']:.3f} est at ${sm['usd_hr']:.3f}/h",
                  f"  productive (jobs)      {sm['productive_s']:.0f}s = {sm['productive_pct']}%   non-productive {sm['idle_overhead_s']:.0f}s = ${sm['idle_usd_est']:.3f}",
                  f"    boot {f(p.get('boot_s'))} | wait before first job {f(p.get('wait_before_first_job_s'))} | "
                  f"gaps between jobs {f(p.get('gaps_between_jobs_s'))} | after last job {f(p.get('after_last_job_s'))}"]
    return "\n".join(lines)
