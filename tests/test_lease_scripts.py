"""Runs the real lease/*.sh scripts in a sandbox: temp HOME/work/run dirs, a local git repo standing in for the
GitHub state repo, real gpg.  No network, no real directories.  Skipped (with the reason) where bash, gpg, git,
python3, flock, tar or gzip are missing, e.g. stock Git Bash on Windows (no flock): run it under WSL/Linux."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

LEASE = Path(__file__).resolve().parent.parent / "lease"
FIX = Path(__file__).resolve().parent / "fixtures" / "lease"
NEEDS = ("git", "gpg", "python3", "flock", "tar", "split", "sha256sum", "gzip", "hostname", "mktemp")


def _find_bash() -> str | None:
    b = shutil.which("bash")
    if os.name == "nt" and (not b or "system32" in b.lower()):   # System32\bash.exe is the WSL launcher
        b = next((p for p in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe") if Path(p).exists()), None)
    return b


BASH = _find_bash()


def px(p: Path | str) -> str:
    """Path as bash sees it (only differs for Git Bash / MSYS on Windows)."""
    s = str(p).replace("\\", "/")
    return re.sub(r"^([A-Za-z]):", lambda m: "/" + m.group(1).lower(), s) if os.name == "nt" else s


def _missing_tools() -> list[str]:
    if not BASH:
        return ["bash"]
    out = subprocess.run([BASH, "-c", "for t in %s; do command -v $t >/dev/null || echo $t; done" % " ".join(NEEDS)],
                         capture_output=True, text=True).stdout
    return out.split()


MISSING = _missing_tools()
needs_sandbox = pytest.mark.skipif(bool(MISSING), reason=f"lease scripts need {', '.join(MISSING)} (run under WSL/Linux)")
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(*args: str, cwd: Path | str | None = None, check: bool = True) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and cp.returncode:
        raise AssertionError(f"git {args}: {cp.stderr}")
    return cp.stdout.strip()


def heads(remote: Path) -> dict[str, str]:
    return {r.split("\t")[1].removeprefix("refs/heads/"): r.split("\t")[0] for r in git("ls-remote", "--heads", str(remote)).splitlines()}


def snap(repo: Path) -> dict:
    """HEAD and git status (for a repo) and a digest of every file outside .git."""
    files = {}
    for p in sorted(repo.rglob("*")):
        if ".git" not in p.relative_to(repo).parts and p.is_file():
            files[str(p.relative_to(repo))] = hashlib.sha256(p.read_bytes()).hexdigest()
    if not (repo / ".git").exists():
        return {"files": files}
    return {"head": git("rev-parse", "HEAD", cwd=repo), "status": sorted(git("status", "--porcelain", cwd=repo).splitlines()), "files": files}


class Box:
    """One fake lease: its own HOME, /work, /run/ddc and state clone, all under a short temp dir (gpg socket paths)."""

    def __init__(self, remote: Path, dseq: str = "100"):
        self.root = Path(tempfile.mkdtemp(prefix="ddc-"))
        r = self.root
        self.home, self.work, self.run_dir, self.sr, self.tmp, self.bin = (r / n for n in ("h", "w", "r", "s", "t", "bin"))
        for d in (self.home / ".config" / "ddc", self.work, self.run_dir, self.tmp, self.bin, self.root / "g"):
            d.mkdir(parents=True)
        (self.root / "g").chmod(0o700)
        (self.home / ".config" / "ddc" / "backup.pass").write_text("test-passphrase")
        self.dseq = dseq
        self.set_remote(remote)

    def set_remote(self, remote: Path | str) -> None:
        url = remote if str(remote).startswith("file://") else "file://" + px(remote)
        (self.home / ".config" / "ddc" / "env").write_text(
            f'export DDC_STATE_REMOTE="{url}"\nexport DDC_DSEQ="{self.dseq}"\nexport DDC_REPOS="repoA plain"\n')

    def env(self, extra: dict | None = None, legacy: bool = False) -> dict:
        e = {**os.environ, **GIT_ENV, "HOME": px(self.home), "DDC_WORK": px(self.work), "DDC_RUN": px(self.run_dir),
             "DDC_STATE_CLONE": px(self.sr), "TMPDIR": px(self.tmp), "GNUPGHOME": px(self.root / "g"),
             "DDC_PTS_GLOB": px(self.root / "no-terminals" / "[0-9]*")}   # a terminal the developer has open must not make the sandboxed agent look busy
        e["PATH"] = px(self.bin) + os.pathsep + e["PATH"]
        return {**e, **(extra or {})}

    def run(self, script: str, *args: str, extra: dict | None = None, legacy: bool = False, timeout: int = 300):
        path = (self.root / "legacy" / script) if legacy else LEASE / script
        return subprocess.run([BASH, px(path), *args], capture_output=True, text=True, env=self.env(extra), timeout=timeout)

    def install_legacy(self) -> None:
        """The first-release checkpoint.sh/restore.sh, with their hard-coded lease paths pointed into this sandbox."""
        (self.root / "legacy").mkdir(exist_ok=True)
        for n in ("checkpoint", "restore"):
            s = (FIX / f"legacy-{n}.sh").read_text(encoding="utf-8")
            for a, b in (("/var/tmp/ddc-state", px(self.sr)), ("/var/tmp/ddc-ck.", px(self.tmp) + "/ddc-ck."), ("/run/ddc", px(self.run_dir)),
                         ("/tmp/ddc-clone.err", px(self.tmp) + "/ddc-clone.err"), ("/work", px(self.work))):
                s = s.replace(a, b)
            (self.root / "legacy" / f"{n}.sh").write_text(s, newline="\n")
        pigz = self.bin / "pigz"   # the legacy scripts call pigz, which a stock distro may not have
        pigz.write_text('#!/bin/sh\ncase "$1" in -1) exec gzip -1;; -dc) shift; exec gzip -dc "$@";; esac\n', newline="\n")
        pigz.chmod(0o755)

    # -- fixtures of a working lease
    def seed(self) -> None:
        """A restored lease with a git repo (dirty + untracked), a plain dir, results, Claude state, shell config."""
        a = self.work / "repoA"
        a.mkdir()
        git("init", "-q", "-b", "main", cwd=a)
        for i in range(12):
            (a / f"f{i}.txt").write_text(f"file {i}\n" * 50)
        (a / "sub").mkdir()
        (a / "sub" / "x.py").write_text("print('x')\n")
        git("add", "-A", cwd=a)
        git("commit", "-qm", "one", cwd=a)
        (a / "f1.txt").write_text("modified\n")
        (a / "untracked.txt").write_text("not in git\n")
        (self.work / "plain").mkdir()
        for i in range(6):
            (self.work / "plain" / f"p{i}").write_text(str(i) * 100)
        (self.work / "results").mkdir(exist_ok=True)
        (self.work / "results" / "r.log").write_text("result\n")
        (self.home / ".claude" / "projects" / "p").mkdir(parents=True)
        (self.home / ".claude" / "projects" / "p" / "s.jsonl").write_text('{"x":1}\n')
        (self.home / ".claude" / ".credentials.json").write_text("SECRET-TOKEN")
        (self.home / ".bash_history").write_text("ls\n")
        (self.home / ".gitconfig").write_text("[user]\n\tname = t\n")
        (self.work / ".ddc-restored").touch()

    def ckpt(self, reason: str = "manual", *args: str, **kw):
        return self.run("checkpoint.sh", reason, *args, **kw)

    def cleanup(self) -> None:
        for _ in range(3):   # a killed restore leaves gpg children that may still (re)start the agent
            subprocess.run(["gpgconf", "--homedir", px(self.root / "g"), "--kill", "gpg-agent"], capture_output=True, env=self.env())
            shutil.rmtree(self.root, ignore_errors=True)
            if not self.root.exists():
                break
            time.sleep(0.5)


@pytest.fixture
def remote(tmp_path):
    r = Path(tempfile.mkdtemp(prefix="ddc-remote-")) / "state.git"
    git("init", "-q", "--bare", "-b", "main", str(r))
    yield r
    shutil.rmtree(r.parent, ignore_errors=True)


@pytest.fixture
def boxes(remote):
    made: list[Box] = []

    def make(dseq: str = "100", rem: Path | None = None) -> Box:
        b = Box(rem or remote, dseq)
        made.append(b)
        return b

    yield make
    for b in made:
        b.cleanup()


def ok(cp, what=""):
    assert cp.returncode == 0, f"{what} rc={cp.returncode}\nstdout={cp.stdout}\nstderr={cp.stderr}"
    return cp


def good_checkpoint(boxes):
    a = boxes("100")
    a.seed()
    ok(a.ckpt("first"), "first checkpoint")
    return a


# ---------------------------------------------------------------- roundtrip

@needs_sandbox
def test_checkpoint_roundtrip_restores_head_status_and_untracked(boxes, remote):
    a = boxes("100")
    out = ok(a.run("restore.sh"), "restore on empty state repo").stdout.strip().splitlines()[-1]
    assert out == "restored= missing=all"
    a.seed()
    cp = ok(a.ckpt("first"))
    assert cp.stdout.strip().splitlines()[-1].startswith("checkpoint_ok ")
    m = json.loads(git("show", "main:manifest.json", cwd=a.sr))
    assert set(m) == {"created_at", "reason", "dseq", "host", "repos", "archives"}
    assert set(m["archives"]) == {"ws-repoA", "ws-plain", "results", "claude", "config"}
    assert m["repos"]["repoA"]["head"] == git("rev-parse", "HEAD", cwd=a.work / "repoA")
    assert m["dseq"] == "100" and m["archives"]["ws-repoA"]["parts"][0]["file"] == "ws-repoA.tar.gz.gpg.part000"
    assert not (a.run_dir / "checkpoint_failed").exists()
    first = heads(remote)["main"]

    b = boxes("200")
    out = ok(b.run("restore.sh"), "restore on fresh lease").stdout.strip().splitlines()[-1]
    assert out.startswith("restored=ws-plain,ws-repoA,results,claude,config missing= manifest=")
    assert snap(b.work / "repoA") == snap(a.work / "repoA")
    assert (b.work / "repoA" / "untracked.txt").read_text() == "not in git\n"
    assert snap(b.work / "plain") == snap(a.work / "plain")
    assert (b.home / ".claude" / "projects" / "p" / "s.jsonl").exists() and not (b.home / ".claude" / ".credentials.json").exists()
    assert (b.work / ".ddc-restored").exists() and not (b.work / ".ddc" / "restore-started").exists()
    assert (b.work / ".ddc" / "complete" / "repoA").exists() and not (b.work / ".ddc" / "stage").exists()

    cp = ok(b.ckpt("second"), "checkpoint from the restored lease")
    h = heads(remote)
    assert h["prev"] == first and h["main"] != first and set(h) == {"main", "prev"}
    assert cp.stdout.strip().splitlines()[-1].split()[2] == h["main"]


@needs_sandbox
def test_periodic_checkpoint_skips_when_nothing_changed(boxes, remote):
    a = good_checkpoint(boxes)
    before = heads(remote)["main"]
    cp = ok(a.ckpt("periodic"))
    assert "checkpoint_skip unchanged" in cp.stdout and heads(remote)["main"] == before


# ---------------------------------------------------------------- compatibility with the first-release scripts

@needs_sandbox
def test_old_format_checkpoint_restores_with_current_restore(boxes, remote):
    old = boxes("100")
    old.install_legacy()
    old.seed()
    ok(old.run("checkpoint.sh", "legacy", legacy=True), "legacy checkpoint")
    new = boxes("200")
    out = ok(new.run("restore.sh"), "current restore of a legacy checkpoint").stdout.strip().splitlines()[-1]
    assert "restored=" in out and "ws-repoA" in out and " missing= " in out
    assert snap(new.work / "repoA") == snap(old.work / "repoA")
    assert snap(new.work / "plain") == snap(old.work / "plain")
    ok(new.ckpt("again"), "current checkpoint on top of a legacy one")


@needs_sandbox
def test_current_checkpoint_restores_with_old_restore(boxes, remote):
    new = good_checkpoint(boxes)
    old = boxes("200")
    old.install_legacy()
    out = ok(old.run("restore.sh", legacy=True), "legacy restore of a current checkpoint").stdout.strip().splitlines()[-1]
    assert "ws-repoA" in out
    assert snap(old.work / "repoA") == snap(new.work / "repoA")
    assert (old.home / ".claude" / "projects" / "p" / "s.jsonl").exists()
    # a lease restored by the old script can checkpoint with the new one, and the old script can still write the layout
    ok(old.ckpt("current-after-legacy-restore"), "current checkpoint on a legacy-restored lease")
    ok(old.run("checkpoint.sh", "legacy-again", legacy=True), "legacy checkpoint over a current one")


# ---------------------------------------------------------------- interrupted / partial restore (SAF-03)

def _tar_shim(box: Box) -> None:
    """`tar -x` extracts only the first bytes of the stream, then SIGKILLs the restore script (power loss mid-extract)."""
    real = shutil.which("tar")
    shim = box.bin / "tar"
    shim.write_text(f'#!/bin/sh\ncase " $* " in *" -xf "*) head -c 3000 | {real} "$@"; kill -9 $PPID ;; *) exec {real} "$@" ;; esac\n', newline="\n")
    shim.chmod(0o755)


@needs_sandbox
def test_interrupted_restore_never_replaces_the_good_checkpoint(boxes, remote):
    a = good_checkpoint(boxes)
    good = heads(remote)
    b = boxes("200")
    _tar_shim(b)
    cp = b.run("restore.sh")
    assert cp.returncode != 0, "restore was killed mid-extract"
    assert not (b.work / "repoA").exists(), "no half-extracted workspace may appear at its final path"
    assert not (b.work / ".ddc-restored").exists() and (b.work / ".ddc" / "restore-started").exists()

    # whatever the agent or the owner tries now, the good checkpoint survives
    for reason in ("periodic", "final-idle", "final-stop"):
        cp = b.ckpt(reason)
        assert cp.returncode != 0 and "restore" in cp.stderr
    (b.work / ".ddc-restored").touch()   # even a stale/forged marker next to restore-started is refused
    assert b.ckpt("final-stop").returncode != 0
    assert heads(remote) == good

    (b.work / ".ddc-restored").unlink()
    (b.bin / "tar").unlink()
    ok(b.run("restore.sh"), "re-run restore")
    assert snap(b.work / "repoA") == snap(a.work / "repoA")
    ok(b.ckpt("after-recovery"))
    assert heads(remote)["prev"] == good["main"]


def _corrupt_remote(remote: Path, mutate) -> Path:
    """A second bare repo holding the checkpoint of `remote` after mutate(clone_dir)."""
    clone = Path(tempfile.mkdtemp(prefix="ddc-clone-")) / "c"
    git("clone", "-q", "file://" + px(remote), str(clone))
    mutate(clone)
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "corrupt", cwd=clone)
    bad = Path(tempfile.mkdtemp(prefix="ddc-bad-")) / "bad.git"
    git("init", "-q", "--bare", "-b", "main", str(bad))
    git("push", "-q", "file://" + px(bad), "main:main", cwd=clone)
    shutil.rmtree(clone.parent, ignore_errors=True)
    return bad


@needs_sandbox
@pytest.mark.parametrize("how,msg", [("truncate", "sha256 mismatch"), ("entries", "entry count mismatch")])
def test_restore_rejects_damaged_checkpoint_and_stays_unrestored(boxes, remote, how, msg):
    good_checkpoint(boxes)

    def mutate(c: Path):
        if how == "truncate":
            p = c / "archives" / "ws-repoA.tar.gz.gpg.part000"
            p.write_bytes(p.read_bytes()[: p.stat().st_size // 2])
        else:
            m = json.loads((c / "manifest.json").read_text())
            m["archives"]["ws-repoA"]["entries"] += 1
            (c / "manifest.json").write_text(json.dumps(m))

    bad = _corrupt_remote(remote, mutate)
    try:
        b = boxes("200", rem=bad)
        cp = b.run("restore.sh")
        assert cp.returncode != 0 and msg in cp.stderr
        assert not (b.work / "repoA").exists() and not (b.work / ".ddc-restored").exists()
        assert b.ckpt("final-stop").returncode != 0
    finally:
        shutil.rmtree(bad.parent, ignore_errors=True)


@needs_sandbox
def test_restore_rejects_wrong_git_head(boxes, remote):
    good_checkpoint(boxes)
    bad = _corrupt_remote(remote, lambda c: (c / "manifest.json").write_text(
        (c / "manifest.json").read_text().replace(json.loads((c / "manifest.json").read_text())["repos"]["repoA"]["head"], "1" * 40)))
    try:
        b = boxes("200", rem=bad)
        cp = b.run("restore.sh")
        assert cp.returncode != 0 and "HEAD" in cp.stderr and not (b.work / "repoA").exists() and not (b.work / ".ddc-restored").exists()
        ok(b.run("restore.sh", extra={"DDC_RESTORE_ALLOW_HEAD_MISMATCH": "1"}), "explicit override")
        assert (b.work / "repoA").exists() and (b.work / ".ddc-restored").exists()
    finally:
        shutil.rmtree(bad.parent, ignore_errors=True)


@needs_sandbox
def test_restore_keeps_existing_targets_and_force_moves_them_aside(boxes, remote):
    a = good_checkpoint(boxes)
    b = boxes("200")
    (b.work / "repoA").mkdir()
    (b.work / "repoA" / "mine.txt").write_text("cloud-only work")
    ok(b.run("restore.sh"))
    assert (b.work / "repoA" / "mine.txt").read_text() == "cloud-only work" and not (b.work / "repoA" / "f0.txt").exists()
    ok(b.run("restore.sh", "--force"))
    assert snap(b.work / "repoA") == snap(a.work / "repoA")
    kept = list((b.work / ".ddc" / "replaced").glob("repoA.*"))
    assert len(kept) == 1 and (kept[0] / "mine.txt").read_text() == "cloud-only work"


# ---------------------------------------------------------------- checkpoint refusals (SAF-03 / SAF-11 / SAF-21)

@needs_sandbox
def test_checkpoint_refuses_without_completed_restore(boxes, remote):
    a = boxes("100")
    a.seed()
    (a.work / ".ddc-restored").unlink()
    cp = a.ckpt()
    assert cp.returncode != 0 and "restore not finished" in cp.stderr and heads(remote) == {}


@needs_sandbox
def test_checkpoint_refuses_emptied_or_missing_workspace(boxes, remote):
    a = good_checkpoint(boxes)
    good = heads(remote)
    for p in (a.work / "repoA").iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    cp = a.ckpt("final-stop")
    assert cp.returncode != 0 and "ws-repoA" in cp.stderr and "--allow-shrink" in cp.stderr
    assert heads(remote) == good
    shutil.rmtree(a.work / "plain")
    cp = a.ckpt("final-stop")
    assert cp.returncode != 0 and "ws-plain (missing)" in cp.stderr and heads(remote) == good
    ok(a.ckpt("final-stop", "--allow-shrink"), "deliberate shrink")
    assert heads(remote)["main"] != good["main"]


@needs_sandbox
def test_unreadable_file_fails_loudly_unless_opted_in(boxes, remote):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read everything")
    a = good_checkpoint(boxes)
    good = heads(remote)
    secret = a.work / "repoA" / "locked.txt"
    secret.write_text("x")
    secret.chmod(0)
    try:
        cp = a.ckpt("manual")
        assert cp.returncode != 0 and "unreadable" in cp.stderr and heads(remote) == good
        ok(a.ckpt("manual", extra={"DDC_CHECKPOINT_IGNORE_UNREADABLE": "1"}))
    finally:
        secret.chmod(stat.S_IRUSR | stat.S_IWUSR)


@needs_sandbox
def test_checkpoint_refuses_foreign_state_repo(boxes, remote, tmp_path):
    # a real project repo (branch master, no manifest) mistakenly configured as the state repo
    work = Path(tempfile.mkdtemp(prefix="ddc-proj-"))
    git("init", "-q", "-b", "master", cwd=work)
    (work / "README.md").write_text("my project")
    git("add", "-A", cwd=work)
    git("commit", "-qm", "init", cwd=work)
    git("push", "-q", "file://" + px(remote), "master:master", cwd=work)
    before = heads(remote)
    for script in ("checkpoint.sh", "restore.sh"):
        a = boxes("100")
        a.seed()
        cp = a.run(script)
        assert cp.returncode != 0 and "not a ddc state repo" in cp.stderr, script
    assert heads(remote) == before
    # same with a repo that only has a main branch without manifest.json
    git("push", "-q", "file://" + px(remote), "--delete", "master", cwd=work)
    git("push", "-q", "file://" + px(remote), "master:main", cwd=work)
    before = heads(remote)
    for script in ("checkpoint.sh", "restore.sh"):
        a = boxes("100")
        a.seed()
        cp = a.run(script)
        assert cp.returncode != 0 and "manifest.json" in cp.stderr, script
    assert heads(remote) == before
    shutil.rmtree(work, ignore_errors=True)


@needs_sandbox
def test_a_valid_state_repo_with_a_foreign_extra_branch_is_still_refused(boxes, remote):
    # the identity check has two halves: "no main" (above) and "any branch besides main/prev". Here main is a genuine checkpoint, so only the second half can refuse.
    a = good_checkpoint(boxes)
    git("branch", "feature-x", "main", cwd=remote)
    before = heads(remote)
    assert {"main", "feature-x"} <= set(before)
    for script_run in (lambda: a.ckpt("again"), lambda: boxes("200").run("restore.sh")):
        cp = script_run()
        assert cp.returncode != 0 and "other branches (feature-x" in cp.stderr and "not a ddc state repo" in cp.stderr
    assert heads(remote) == before


@needs_sandbox
def test_second_writer_is_refused_unless_adopted(boxes, remote):
    a = good_checkpoint(boxes)                     # lease 100 wrote C1
    b = boxes("200")
    ok(b.run("restore.sh"))
    ok(b.ckpt("from-b"))                           # lease 200 legitimately continues: C2
    c2 = heads(remote)
    cp = a.ckpt("stale")                           # lease 100 never saw C2
    assert cp.returncode != 0 and "not written by this lease" in cp.stderr and heads(remote) == c2
    ok(a.ckpt("adopt", extra={"DDC_ADOPT_REMOTE": "1"}))
    assert heads(remote)["main"] != c2["main"] and heads(remote)["prev"] == c2["main"]
    # a lease that lost its local clone but wrote the remote main itself carries on without the override
    shutil.rmtree(a.sr)
    ok(a.ckpt("clone-lost"))


# ---------------------------------------------------------------- unreachable state repo / failed upload (SAF-11, SAF-25)

@needs_sandbox
def test_unreachable_state_repo_fails_loudly_and_leaves_local_state_alone(boxes, remote, tmp_path):
    a = good_checkpoint(boxes)
    last = (a.run_dir / "last_checkpoint").read_text()
    main_before = git("rev-parse", "main", cwd=a.sr)
    a.set_remote("file://" + px(tmp_path / "nope.git"))
    (a.work / "repoA" / "new.txt").write_text("new work")
    cp = a.ckpt("final-stop")
    assert cp.returncode != 0 and "cannot reach the state repo" in cp.stderr
    assert (a.work / "repoA" / "new.txt").read_text() == "new work" and (a.run_dir / "last_checkpoint").read_text() == last
    assert git("rev-parse", "main", cwd=a.sr) == main_before and "ws-repoA" in git("show", "main:manifest.json", cwd=a.sr)
    # restore treats it as an error too, never as "no checkpoint yet"
    b = boxes("200", rem=tmp_path / "nope.git")
    cp = b.run("restore.sh")
    assert cp.returncode != 0 and "cannot reach" in cp.stderr and "missing=all" not in cp.stdout and not (b.work / ".ddc-restored").exists()


@needs_sandbox
def test_failed_upload_keeps_local_copy_and_remote_untouched(boxes, remote):
    a = good_checkpoint(boxes)
    good = heads(remote)
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: simulated outage' >&2\nexit 1\n", newline="\n")
    hook.chmod(0o755)
    (a.work / "repoA" / "new.txt").write_text("new work")
    cp = a.ckpt("final-stop")
    assert cp.returncode != 0 and "upload FAILED" in cp.stderr and "ck-new" in cp.stderr
    assert heads(remote) == good
    assert git("rev-parse", "main", cwd=a.sr) == good["main"], "local previous checkpoint untouched"
    assert "refs/heads/ck-new" in git("for-each-ref", "--format=%(refname)", cwd=a.sr)
    # the retained local checkpoint is a complete one (manifest + archives)
    assert "ws-repoA.tar.gz.gpg.part000" in git("ls-tree", "-r", "--name-only", "ck-new", cwd=a.sr)
    hook.unlink()
    ok(a.ckpt("final-stop"), "retry once the remote is back")
    assert heads(remote)["prev"] == good["main"]
    assert "ck-new" not in git("for-each-ref", "--format=%(refname)", cwd=a.sr)


# ---------------------------------------------------------------- idle detection (SAF-08)

def _fake_ps(box: Box, lines: list[str] | None) -> None:
    """A `ps` stub: prints `lines` (pid comm args), or fails when lines is None."""
    body = "exit 1" if lines is None else "cat <<'EOF'\n" + "\n".join(lines) + "\nEOF"
    p = box.bin / "ps"
    p.write_text("#!/bin/sh\n" + body + "\n", newline="\n")
    p.chmod(0o755)


PROBE = "101 bash bash -c # cd:probe vitest run dist.sh bench.sh npm test git push rsync tsc"
BUSY = ["201 node node /w/node_modules/.bin/vitest run", "202 npm npm run build", "203 git git fetch origin", "204 make /usr/bin/make -j4"]


def _busy_rc(box: Box, regex_env: str = "") -> int:
    script = f'. "{px(LEASE)}/ddc-lib.sh"; ddc_busy_procs "$DDC_BUSY_DEFAULT{regex_env}"'
    return subprocess.run([BASH, "-c", script], env=box.env(), capture_output=True, text=True).returncode


@needs_sandbox
def test_busy_scan_ignores_probe_and_wrapper_processes(boxes):
    b = boxes()
    _fake_ps(b, [PROBE, "102 ssh ssh w01 cd /work && vitest run", "103 sshd sshd: dev@notty", "104 watch watch -t -n 5 /opt/ddc/workers.sh",
                 "105 bash bash /opt/ddc/workers.sh", "106 pgrep pgrep -fa stryker run|vitest run|dist.sh", "107 sleep sleep 60"])
    assert _busy_rc(b) == 1
    for line in BUSY:
        _fake_ps(b, [PROBE, line])
        assert _busy_rc(b) == 0, line
    _fake_ps(b, ["300 bash bash /opt/ddc/dist.sh run x jobs.txt"])
    assert _busy_rc(b) == 0, "a script runner is real work"
    _fake_ps(b, ["400 mytool mytool --go"])
    assert _busy_rc(b) == 1 and _busy_rc(b, "|(^|[/ ])mytool( |$)") == 0
    _fake_ps(b, None)
    assert _busy_rc(b) == 2, "a failing scan is reported, not read as idle"


@needs_sandbox
@pytest.mark.parametrize("ps_lines,extra,reason_ok", [
    ([PROBE], {}, lambda r: not r.startswith("procs")),
    (BUSY[:1], {}, lambda r: r == "procs"),
    (["400 mytool mytool --go"], {}, lambda r: not r.startswith("procs")),
    (["400 mytool mytool --go"], {"DDC_BUSY_REGEX": "(^|[/ ])mytool( |$)"}, lambda r: r == "procs"),
    (["400 mytool mytool --go"], {"DDC_BUSY_REGEX": "(unbalanced"}, lambda r: not r.startswith("procs")),   # bad regex ignored, default kept
    (None, {}, lambda r: r == "procs_detect_error"),
])
def test_agent_active_reason(boxes, ps_lines, extra, reason_ok):
    b = boxes()
    _fake_ps(b, ps_lines)
    (b.work / "results").mkdir()
    cp = subprocess.run([BASH, px(LEASE / "agent.sh")], capture_output=True, text=True, timeout=60,
                        env=b.env({"DDC_AGENT_ONCE": "1", **extra}))
    assert cp.returncode == 0, cp.stderr
    st = json.loads((b.run_dir / "state.json").read_text())
    assert reason_ok(st["active_reason"]), st
    assert set(st) == {"now", "started", "uptime_min", "idle_min", "active_reason", "dist_idle_min", "last_checkpoint", "checkpoint_failed", "shutdown_requested"}


@needs_sandbox
def test_dist_marks_worker_activity(boxes):
    b = boxes()
    cp = subprocess.run([BASH, px(LEASE / "dist.sh"), "hosts"], capture_output=True, text=True, env=b.env({"DIST_HOSTS": "w01 w02"}))
    assert cp.stdout.split() == ["w01", "w02"] and (b.run_dir / "worker_activity").exists()


@needs_sandbox
@pytest.mark.skipif(not Path("/usr/bin/time").exists() or not shutil.which("bc"), reason="bench.sh needs /usr/bin/time and bc")
def test_bench_runs_configured_command(boxes):
    b = boxes()
    (b.work / "repoA").mkdir()
    cp = subprocess.run([BASH, px(LEASE / "bench.sh"), "cmd"], capture_output=True, text=True,
                        env=b.env({"DDC_BENCH_CMD": "echo hi", "DDC_REPOS": "repoA"}))
    r = json.loads(cp.stdout.strip().splitlines()[-1])
    assert r["cmd"]["exit"] == 0 and r["cmd"]["wall_s"] >= 0 and "cpu" not in r
    assert (b.work / "results" / "bench-cmd.log").read_text().strip() == "hi"


# ---------------------------------------------------------------- static policy (run everywhere bash exists)

SCRIPTS = sorted(LEASE.glob("*.sh"))


def test_lease_dir_holds_only_generic_tooling():
    assert {p.name for p in LEASE.iterdir()} == {"agent.sh", "bench.sh", "checkpoint.sh", "ddc-lib.sh", "dist.sh", "README.md",
                                                 "restore.sh", "setup.sh", "tmux.conf", "workers.sh"}


@pytest.mark.parametrize("p", sorted(LEASE.iterdir()), ids=lambda p: p.name)
def test_lease_files_are_lf_and_project_neutral(p):
    data = p.read_bytes()
    assert b"\r" not in data
    text = data.decode("utf-8")
    assert not re.search(r"drill|stryker(?!-tmp)", text, re.I), p.name


@pytest.mark.skipif(not BASH, reason="no bash")
@pytest.mark.parametrize("p", SCRIPTS, ids=lambda p: p.name)
def test_lease_scripts_parse(p):
    assert subprocess.run([BASH, "-n", px(p)], capture_output=True, text=True).returncode == 0


def test_lease_scripts_are_executable_in_git():
    """agent.sh runs checkpoint.sh directly (setup uploads them as 0755): a fresh Linux clone only keeps that if git records the exec bit.  The same holds for the
    scripts `ddc job` ships to a job's box (jobremote/): step.sh and fetch.sh are run as files, boot.sh is inlined into the SDL but stays a normal script."""
    git_bin = shutil.which("git")
    if not git_bin or not (LEASE.parent / ".git").exists():
        pytest.skip("not a git checkout")
    for d in ("lease", "jobremote"):
        out = subprocess.run([git_bin, "ls-files", "-s", d], cwd=LEASE.parent, capture_output=True, text=True).stdout
        modes = {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}
        assert modes and [n for n, m in modes.items() if n.endswith(".sh") and m != "100755"] == [], d


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_lease_scripts_pass_shellcheck():
    cp = subprocess.run(["shellcheck", "-x", "-S", "warning", *map(str, SCRIPTS)], capture_output=True, text=True, cwd=LEASE)
    assert cp.returncode == 0, cp.stdout


def test_setup_has_no_unverified_downloads_and_pins_claude_code():
    s = (LEASE / "setup.sh").read_text(encoding="utf-8")
    everything = "\n".join(p.read_text(encoding="utf-8") for p in SCRIPTS)
    assert not re.search(r"curl[^\n]*\|\s*(ba)?sh", everything) and "claude.ai/install" not in everything
    for line in s.splitlines():
        if "npm install" in line:
            assert "@anthropic-ai/claude-code@$ver" in line and "DDC_INSTALL_CLAUDE_CODE" in s
    assert re.search(r'ver=\$\{ver:-\d+\.\d+\.\d+\}', s) and "DDC_INSTALL_CLAUDE_CODE" in s


@pytest.mark.parametrize("name", ["checkpoint.sh", "restore.sh", "agent.sh", "setup.sh", "dist.sh"])
def test_scripts_never_trace_secrets(name):
    assert re.search(r"^set \+x", (LEASE / name).read_text(encoding="utf-8"), re.M)
