"""`ddc runner up|down`: ephemeral GitHub Actions runners on an already running dev box, as the unprivileged box user `ci`.

Never starts a lease.  The GitHub side runs on this PC through the logged-in `gh` CLI (its token never leaves gh); the box only ever
receives one single-use, encoded JIT config per runner, over ssh stdin, read by lease/runner.sh.  Private repos only: a self-hosted
runner on a public repo would run code from anyone's pull request.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from . import controller as ddc

LABELS = ["self-hosted", "Linux", "X64", "proj-ephemeral", "ddc"]
REPO = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
WATCH_S = 30


def gh(path: str, method: str = "GET", body: dict[str, Any] | None = None, ok: tuple[int, ...] = ()) -> Any:
    """`gh api` call.  The request body goes over stdin; stderr is returned to the caller only as an HTTP status hint."""
    args = ["gh", "api", "-X", method, path] + (["--input", "-"] if body is not None else [])
    cp = subprocess.run(args, input=json.dumps(body).encode() if body is not None else None, capture_output=True, timeout=60,
                        creationflags=ddc.NOWIN)
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace")
        code = next((c for c in ok if f"HTTP {c}" in err), None)
        if code:
            return code
        ddc.die(f"gh api {method} {path}: {err.strip()[-300:]}")
    out = cp.stdout.decode("utf-8", "replace").strip()
    return json.loads(out) if out else None


def _repos(repos: list[str]) -> list[str]:
    for r in repos:
        if not REPO.fullmatch(r):
            ddc.die(f"not an owner/repo: {r!r}")
    return repos


def _ours(rn: dict[str, Any]) -> bool:
    return rn.get("name", "").startswith("ddc-") and "ddc" in {lb.get("name") for lb in rn.get("labels", [])}


def _box() -> dict[str, Any]:
    d = ddc.dev_box()
    if not d:
        ddc.die("no dev box is up (`ddc status`); `ddc runner` never starts a lease")
    return d


def _root(d: dict[str, Any], cmd: str, input: bytes | None = None, timeout: int = 600) -> str:
    return ddc.ssh(d, cmd, user="root", input=input, timeout=timeout)


def _saved() -> dict[str, int]:
    return ddc.read_json(ddc.STATE_DIR / "runners.json", {})


def up_pass(d: dict[str, Any], want: dict[str, int]) -> list[str]:
    """One refresh: every wanted slot without a live runner on the box gets a fresh JIT runner.  Returns the slots started."""
    _root(d, "bash /opt/ddc/runner.sh install", timeout=900)
    alive = set(_root(d, "bash /opt/ddc/runner.sh list").split())
    started = []
    for repo, n_slots in want.items():
        for n in range(1, n_slots + 1):
            slot, name = f"{repo.replace('/', '.')}.{n}", f"ddc-{d['dseq']}-{n}"
            if slot in alive:
                continue
            body = {"name": name, "runner_group_id": 1, "labels": LABELS, "work_folder": "_work"}
            res = gh(f"repos/{repo}/actions/runners/generate-jitconfig", "POST", body, ok=(409,))
            if res == 409:   # a registration with this name is left over (box died mid-job, or the runner is still unregistering)
                stale = [r for r in gh(f"repos/{repo}/actions/runners?per_page=100")["runners"] if r["name"] == name and _ours(r)]
                if stale and not stale[0]["busy"]:
                    gh(f"repos/{repo}/actions/runners/{stale[0]['id']}", "DELETE", ok=(404,))
                ddc.log(f"{repo}: {name} is still registered; retrying on the next pass")
                continue
            # the JIT config is the only secret here: in memory and on the ssh pipe, never in an argv or a file on this PC
            _root(d, f"bash /opt/ddc/runner.sh start {slot}", input=res["encoded_jit_config"].encode() + b"\n")
            started.append(slot)
    return started


def cmd_up(repos: list[str], slots: int, watch: bool) -> None:
    if not 1 <= slots <= 16:
        ddc.die("--slots must be 1..16")
    d = _box()   # checked before any GitHub call
    want = {r: slots for r in _repos(repos)}
    for r in want:
        if gh(f"repos/{r}").get("private") is not True:
            ddc.die(f"{r} is public (or unreadable): self-hosted runners would run untrusted pull-request code; keep it on GitHub-hosted runners")
    ok = _root(d, "id ci >/dev/null && test -x /opt/ddc/runner.sh && echo ok", timeout=60).strip()
    if ok != "ok":
        ddc.die("this dev box was provisioned before runner support (no `ci` user); it gets it at the next `ddc start`")
    ddc.write_json(ddc.STATE_DIR / "runners.json", _saved() | want)
    fails = 0
    while True:
        try:
            for s in up_pass(d, want):
                ddc.log(f"runner started: {s}")
            fails = 0
        except (ddc.DdcError, subprocess.SubprocessError, OSError) as e:
            if not watch:
                raise
            fails += 1
            ddc.log(f"runner refresh failed ({fails}/3): {e}")
            if fails >= 3:
                ddc.die("dev box unreachable three times in a row; stopping the runner loop")
        if not watch:
            return
        time.sleep(WATCH_S)
        nd = ddc.dev_box()
        if not nd or nd["dseq"] != d["dseq"]:
            ddc.log("dev box is gone: runner loop ends (queued jobs fall back as configured in the repos)")
            return


def cmd_down(repos: list[str], force: bool) -> None:
    saved = _saved()
    for r in _repos(repos or list(saved)):
        for rn in gh(f"repos/{r}/actions/runners?per_page=100")["runners"]:
            if not _ours(rn):
                continue
            if rn["busy"] and not force:
                ddc.log(f"{r}: {rn['name']} is running a job; left to finish (it is ephemeral)")
                continue
            gh(f"repos/{r}/actions/runners/{rn['id']}", "DELETE", ok=(404,))
            ddc.log(f"{r}: removed {rn['name']}")
        saved.pop(r, None)
    ddc.write_json(ddc.STATE_DIR / "runners.json", saved)
    d = ddc.dev_box()
    if d:
        ddc.log(_root(d, "bash /opt/ddc/runner.sh stop" + (" --force" if force else "")).strip() or "box: no runner processes")


def main(a: Any) -> int:
    if a.runner_cmd == "up":
        cmd_up(a.repos, a.slots, a.watch)
    else:
        cmd_down(a.repos, a.force)
    return 0
