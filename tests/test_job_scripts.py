"""The scripts `ddc job` ships to a job's box (jobremote/): run for real under bash against a local directory standing in for /work (only ssh and
the Akash API are replaced), plus the static policy for them (exec bit in git, LF, shellcheck, shipped in the wheel).  Tests that need bash skip
where it is missing (Git Bash on Windows has it; WSL/Linux has the real thing)."""
from __future__ import annotations

import contextlib
import functools
import hashlib
import http.server
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import pytest
from job_fakes import FakeAkash, install
from job_helpers import make_spec
from test_lease_scripts import BASH, px

from clouddeck.engine import ddc
from clouddeck.job import spec as specmod
from clouddeck.job.clock import FakeClock, RealClock
from clouddeck.job.remote import SshRemote
from clouddeck.job.runner import JobRunner

ROOT = Path(__file__).resolve().parent.parent
JOBREMOTE = ROOT / "jobremote"
SH = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
needs_bash = pytest.mark.skipif(not BASH, reason="needs bash")


@contextlib.contextmanager
def http_dir(directory: Path):
    """A throw-away http server on 127.0.0.1 serving `directory` (fetch.sh only accepts http(s) sources).  Yields url(name)."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    handler.log_message = lambda *a, **k: None   # type: ignore[attr-defined]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield lambda name: f"http://127.0.0.1:{srv.server_address[1]}/{name}"
    finally:
        srv.shutdown()
        srv.server_close()


DEAD = "http://127.0.0.1:9/nothing"   # nothing listens on the discard port: connection refused


class LocalBash:
    """SshRemote with the ssh hop replaced by a local bash: exercises the real command strings and the real step/fetch scripts."""

    def __new__(cls, work: Path, clock):
        class R(SshRemote):
            def _argv(self, cmd):
                return [BASH, "-c", f"export DDC_WORK='{px(work)}'; cd '{px(work)}'; {cmd}"]

        r = R("localhost", 22, Path("k"), Path("kh"), clock)
        r.work = px(work)
        return r


@needs_bash
def test_step_script_is_idempotent_waits_for_needs_and_records_failure(tmp_path):
    r = LocalBash(tmp_path, RealClock())
    r.install_scripts()
    r.start("second", ["first"], "echo two > two.txt")
    time.sleep(1.5)
    assert "second" not in {k for k, v in r.markers().items() if v.get("state") == "done"}   # blocked on `first`
    r.start("first", [], "echo one > one.txt")
    for _ in range(40):
        m = r.markers()
        if m.get("second", {}).get("state") == "done":
            break
        time.sleep(0.5)
    assert m["first"]["state"] == "done" and m["second"]["state"] == "done" and m["second"]["start"] >= m["first"]["end"] - 0.01
    before = (tmp_path / ".ddc" / "first.done").read_text()
    r.start("first", [], "echo SHOULD-NOT-RUN > one.txt")
    time.sleep(1)
    assert (tmp_path / "one.txt").read_text().strip() == "one" and (tmp_path / ".ddc" / "first.done").read_text() == before
    r.start("bad", [], "exit 7")
    r.start("dependent", ["bad"], "echo never")
    for _ in range(40):
        m = r.markers()
        if m.get("dependent", {}).get("state") == "failed":
            break
        time.sleep(0.5)
    assert m["bad"]["rc"] == 7 and m["dependent"]["state"] == "failed" and not (tmp_path / "never").exists()


@needs_bash
def test_secrets_file_is_private_and_sourced_by_steps_but_never_echoed(tmp_path):
    r = LocalBash(tmp_path, RealClock())
    r.install_scripts()
    r.push_secrets({"EXAMPLE_API_TOKEN": "tok'en with $pace"})
    assert (tmp_path / ".ddc" / "secrets.env").stat().st_mode & 0o077 == 0 or os.name == "nt"      # umask 077
    r.start("show", [], 'test "$EXAMPLE_API_TOKEN" = "tok\'en with \\$pace" && echo seen > seen.txt')
    for _ in range(40):
        if r.markers().get("show", {}).get("state") in ("done", "failed"):
            break
        time.sleep(0.5)
    assert (tmp_path / "seen.txt").exists()                                                          # quoted safely, available to steps
    with pytest.raises(ValueError):
        r.push_secrets({"BAD NAME; rm -rf /": "x"})


@needs_bash
def test_transfer_primitives_resume_and_verify(tmp_path):
    r = LocalBash(tmp_path, RealClock())
    data = os.urandom(300_000)
    assert r.put("d/f.bin", 0, data[:100_000]) and not r.put("d/f.bin", 5, b"x")           # wrong offset refused
    assert r.size("d/f.bin") == (100_000, False)
    assert r.put("d/f.bin", 100_000, data[100_000:]) and not r.finalize("d/f.bin", "0" * 64)   # bad sha: stays .part
    assert r.size("d/f.bin") == (300_000, False) and r.finalize("d/f.bin", SH(data))
    assert r.size("d/f.bin") == (300_000, True)
    assert r.get("d/f.bin", 100_000, 50_000) == data[100_000:150_000]
    assert r.glob(["d/*.bin"]) == [("d/f.bin", 300_000, SH(data))] and r.glob(["nothing/*"]) == []


@needs_bash
def test_glob_refuses_anything_but_plain_relative_patterns_and_drops_forged_rows(tmp_path, monkeypatch):
    r = LocalBash(tmp_path, RealClock())
    for bad in ("out/$(id)", "../x/*", "/etc/*", "a;b", "a b"):
        with pytest.raises(ValueError):
            r.glob([bad])
    forged = "\n".join(["0" * 64 + " 3 ../../escaped.bin", "0" * 64 + " 3 /abs/path.bin", "not-a-sha 3 x.bin", "0" * 64 + " 3 ok/y.bin"])
    monkeypatch.setattr(r, "_sh", lambda cmd, **kw: forged)
    assert r.glob(["ok/*"]) == [("ok/y.bin", 3, "0" * 64)]                                        # the box is not trusted: only well-formed rows below /work survive


@needs_bash
@pytest.mark.skipif(not shutil.which("curl"), reason="needs bash + curl")
def test_fetch_script_falls_back_to_origin_and_rejects_bad_mirror_bytes(tmp_path):
    good = tmp_path / "origin.bin"
    good.write_bytes(b"model-weights" * 1000)
    bad = tmp_path / "mirror.bin"
    bad.write_bytes(b"corrupted!" * 1000)
    script = px(JOBREMOTE / "fetch.sh")
    env = {**os.environ, "DDC_WORK": px(tmp_path / "w")}

    def fetch(sha, *urls):
        return subprocess.run([BASH, script, "models/m.bin", sha, *urls], env=env, capture_output=True, text=True)

    sha = SH(good.read_bytes())
    with http_dir(tmp_path) as url:
        r = fetch(sha, DEAD, url("mirror.bin"), url("origin.bin"))            # dead mirror, lying mirror, then origin
        assert r.returncode == 0 and "via source 3" in r.stdout and (tmp_path / "w" / "models" / "m.bin").read_bytes() == good.read_bytes()
        assert "have" in fetch(sha, DEAD).stdout                              # already present + verified: no network at all
        (tmp_path / "w" / "models" / "m.bin").unlink()
        assert fetch(sha, DEAD, url("mirror.bin")).returncode == 1            # every source bad -> loud failure, no partial file kept
        assert not (tmp_path / "w" / "models" / "m.bin").exists()


@needs_bash
def test_fetch_script_refuses_non_http_sources_options_and_a_malformed_checksum(tmp_path):
    """[7] a source starting with `-` would be read as an option by curl/aria2c; file:// and ftp:// are not wanted; a checksum is 64 hex digits or `-`."""
    src = tmp_path / "x.bin"
    src.write_bytes(b"abc")
    env = {**os.environ, "DDC_WORK": px(tmp_path / "w")}

    def fetch(sha, *urls):
        return subprocess.run([BASH, px(JOBREMOTE / "fetch.sh"), "m.bin", sha, *urls], env=env, capture_output=True, text=True)

    for bad in (src.as_uri(), "ftp://example.com/x", "--output=/tmp/pwned", "-o", "http://", "https://exa mple/x", ""):
        r = fetch("-", bad)
        assert r.returncode == 1 and "refused" in r.stderr and not (tmp_path / "w" / "m.bin").exists(), bad
    for sha in ("", "A" * 64, "a" * 63, "a" * 64 + "x", "nope"):
        r = fetch(sha, "http://127.0.0.1:9/x")
        assert r.returncode == 2 and "SHA256" in r.stderr, sha


@needs_bash
@pytest.mark.skipif(not shutil.which("curl"), reason="needs bash + curl")
def test_end_to_end_with_real_remote_scripts_on_a_local_dir_as_the_box(tmp_path, monkeypatch):
    """Real SshRemote command strings, step.sh, fetch.sh, resumable put/get and sha checks; only ssh and the Akash API are stubbed."""

    class SlowClock(FakeClock):  # virtual time for the controller, a few real ms per sleep so real background steps can progress
        def sleep(self, s):
            super().sleep(s)
            time.sleep(0.03)

    clock = SlowClock(time.time())
    akash = FakeAkash(clock, bid_delay_s=5, port_delay_s=2, boot_s={"default": 5})
    install(monkeypatch, clock, akash)
    ddc.CFG.update(max_price_usd_hr=5.0, budget_usd=85.0)
    box = tmp_path / "box"
    box.mkdir()
    model = tmp_path / "model.bin"
    model.write_bytes(b"weights" * 20000)
    inp = tmp_path / "clip.bin"
    inp.write_bytes(os.urandom(2_500_000))
    served = tmp_path / "served"
    served.mkdir()
    (served / "model.bin").write_bytes(model.read_bytes())
    srv_ctx = http_dir(served)
    srv_url = srv_ctx.__enter__()
    s = make_spec(tmp_path, inputs=["clip.bin"], chunk_mb=1, poll_s=1,
                  hardware={"cpu": 2, "memory": "8Gi", "disk": "50Gi", "max_usd_hr": 3.0, "gpu": {"vendor": "vendor-a", "models": ["model-a"]}},
                  cache={"item": [{"name": "m", "dest": "models/m.bin", "urls": [DEAD, srv_url("model.bin")], "sha256": SH(model.read_bytes()), "size_gb": 0.1}]},
                  step=[{"name": "canary", "run": "test -s in/clip.bin && test -s models/m.bin && mkdir -p out && echo ok > out/canary.txt", "artifacts": ["out/canary.txt"]},
                        {"name": "work", "needs": ["canary"], "run": "cat in/clip.bin models/m.bin | sha256sum | cut -d' ' -f1 > out/digest.txt", "artifacts": ["out/digest.txt"]}])

    def factory(host, port):
        r = LocalBash(box, clock)
        r.probe = lambda: True
        r.boot_info = lambda: {}
        return r

    r = JobRunner(s, clock=clock, remote_factory=factory, install_signals=False, log=lambda m: None)
    r.run()
    srv_ctx.__exit__(None, None, None)
    assert akash.deps[r.dseq]["state"] == "closed"
    got = tmp_path / "got" / "out"
    assert (got / "canary.txt").read_text().strip() == "ok"
    assert (got / "digest.txt").read_text().strip() == SH(inp.read_bytes() + model.read_bytes())   # inputs + cached model really met on the "box"
    assert (box / "in" / "clip.bin").read_bytes() == inp.read_bytes()
    t = r.tel.to_dict(r.usd_hr)
    assert t["steps"]["canary"]["end"] <= t["steps"]["work"]["start"] + 0.5 and t["summary"]["productive_s"] > 0


# ------------------------------------------------------------------ static policy
SCRIPTS = sorted(JOBREMOTE.glob("*.sh"))


def test_jobremote_holds_exactly_the_three_scripts():
    assert [p.name for p in SCRIPTS] == ["boot.sh", "fetch.sh", "step.sh"] and {p.name for p in JOBREMOTE.iterdir()} == {"boot.sh", "fetch.sh", "step.sh"}


@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_jobremote_scripts_are_lf_neutral_and_secret_free(p):
    data = p.read_bytes()
    assert b"\r" not in data and data.startswith(b"#!/usr/bin/env bash")
    text = data.decode("utf-8")
    assert not re.search(r"AKASH_API_KEY|BEGIN [A-Z ]*PRIVATE KEY", text, re.I), p.name
    assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", text)                       # nothing is piped into a shell


@pytest.mark.skipif(not BASH, reason="no bash")
@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_jobremote_scripts_parse(p):
    assert subprocess.run([BASH, "-n", px(p)], capture_output=True, text=True).returncode == 0


def test_jobremote_scripts_are_executable_in_git_and_gitattributes_forces_lf():
    git_bin = shutil.which("git")
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.sh\s.*eol=lf", attrs, re.M) or re.search(r"^\*\s.*eol=lf", attrs, re.M)
    if not git_bin or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run([git_bin, "ls-files", "-s", "jobremote"], cwd=ROOT, capture_output=True, text=True).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}
    assert set(modes) == {f"jobremote/{p.name}" for p in SCRIPTS} and set(modes.values()) == {"100755"}, modes


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_jobremote_scripts_pass_shellcheck():
    cp = subprocess.run(["shellcheck", "-x", "-S", "warning", *map(str, SCRIPTS)], capture_output=True, text=True, cwd=JOBREMOTE)
    assert cp.returncode == 0, cp.stdout


def test_boot_script_is_what_the_sdl_runs_and_the_sdl_has_no_secret_values(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_API_TOKEN", "s3cr3t-value-123")
    boot = (JOBREMOTE / "boot.sh").read_text(encoding="utf-8")
    assert "ssh-keygen -A" in boot and "PasswordAuthentication no" in boot and "PermitRootLogin prohibit-password" in boot and "ssh_host_" not in boot.replace("ssh-keygen -A", "")
    s = make_spec(tmp_path, env={"secrets": ["EXAMPLE_API_TOKEN"]})
    text = specmod.render_sdl(s, "ubuntu:24.04", "ssh-ed25519 AAAA k")
    assert "exec /usr/sbin/sshd -D -e" in text and "s3cr3t-value-123" not in text           # the SDL carries the script and only the public key


# ------------------------------------------------------------------ packaging: the wheel provably ships them
def test_check_dist_expects_every_jobremote_script_and_the_job_package(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("check_dist", ROOT / "scripts" / "check_dist.py")
    cd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cd)
    assert {f"clouddeck/data/jobremote/{n}" for n in ("boot.sh", "fetch.sh", "step.sh")} <= set(cd.WHEEL_NEED)
    assert {"clouddeck/job/runner.py", "clouddeck/job/cli.py", "clouddeck/job/__main__.py"} <= set(cd.WHEEL_NEED)
    assert {"jobremote/boot.sh", "docker/Dockerfile", "docs/JOBS.md"} <= set(cd.SDIST_NEED)
    whl = tmp_path / "clouddeck-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl, "w") as z:
        for n in cd.WHEEL_NEED:
            z.writestr(n, "x")
        z.writestr("clouddeck-0.0.0.dist-info/entry_points.txt", "x")
        z.writestr("clouddeck-0.0.0.dist-info/licenses/LICENSE", "x")
    assert cd.check(whl) == []
    short = tmp_path / "clouddeck-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(short, "w") as z:
        for n in cd.WHEEL_NEED:
            if n != "clouddeck/data/jobremote/step.sh":
                z.writestr(n, "x")
        z.writestr("clouddeck-0.0.1.dist-info/entry_points.txt", "x")
        z.writestr("clouddeck-0.0.1.dist-info/licenses/LICENSE", "x")
    assert cd.check(short) == ["missing clouddeck/data/jobremote/step.sh"]


def test_setup_py_and_manifest_ship_jobremote_and_the_package_dir_is_the_only_job_code():
    assert '"jobremote"' in (ROOT / "setup.py").read_text(encoding="utf-8")
    assert "recursive-include jobremote *" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert not (ROOT / "ddcjob").exists() and (ROOT / "clouddeck" / "job" / "runner.py").is_file()
    # a checkout finds the scripts at the repo root; an installed wheel finds them under clouddeck/data (controller.data_file does both)
    assert ddc.data_file("jobremote") == ROOT / "jobremote" and (ddc.data_file("jobremote") / "step.sh").is_file()
