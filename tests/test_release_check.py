"""scripts/release_check.py: catches planted fake secrets/private names/forbidden files (tree and history) and never prints the matched text.

Every fake below is assembled at run time from fragments, so this file contains none of them (it must itself pass the gate)."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release_check.py"
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
if not SCRIPT.exists():  # e.g. tests run from an sdist that does not ship scripts/
    pytest.skip("scripts/release_check.py not present", allow_module_level=True)

spec = importlib.util.spec_from_file_location("release_check", SCRIPT)
rc = importlib.util.module_from_spec(spec)
sys.modules["release_check"] = rc
spec.loader.exec_module(rc)

WORD = "zorb" + "lax"  # a made-up private word; the real ones live in the owner's gitignored scripts/release_denylist.local.txt
FAKES = {  # rule id -> text that must trip it (private-word only with the denylist below)
    "private-word": "the " + WORD + "-core",
    "user-path": "C:" + "\\Users\\" + "jdoe" + "\\work",
    "session-link": "https://claude" + ".ai/code/" + "session_" + "01AbCdEf",
    "akash-address": "akash1" + "q" * 38,
    "private-key": "-----BEGIN OPENSSH " + "PRIVATE" + " KEY-----",
    "github-token": "ghp" + "_" + "A1b2C3d4E5" * 4,
    "api-token": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
    "akash-api-key": "AKASH" + "_API_KEY=" + "abcdef0123456789ZZ",
}
REQUIRED = ["LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", ".env.example", ".gitignore", "docs/ARCHITECTURE.md"]


def git(repo, *args):
    subprocess.run(["git", "-c", "core.longpaths=true", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True)


def put(repo, files):
    for name, text in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def deny(tmp_path):
    d = tmp_path / "deny.txt"
    d.write_text("# private words\n\n" + WORD + "\n" + WORD + "-dev-cloud-state\n4242" + "4242424\n", encoding="utf-8")
    return d


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q")
    put(r, {n: "ok\n" for n in REQUIRED})
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def run(repo, *args, deny=None):
    env = {k: v for k, v in os.environ.items() if k != rc.DENYLIST_ENV} | ({rc.DENYLIST_ENV: str(deny)} if deny else {})
    p = subprocess.run([sys.executable, "-I", str(SCRIPT), "--root", str(repo), *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def test_clean_repo_passes(repo):
    code, out = run(repo)
    assert code == 0 and "0 error(s)" in out, out


@pytest.mark.parametrize("rule", sorted(FAKES))
def test_planted_secret_is_caught_in_tree_without_echo(repo, deny, rule):
    put(repo, {"src/app.py": "x = 1\nnote = '" + FAKES[rule] + "'\n"})
    code, out = run(repo, "--no-history", deny=deny)
    assert code == 1
    assert f"src/app.py:2: {rule}" in out
    assert FAKES[rule] not in out and FAKES[rule].split()[-1] not in out  # location and rule only, never the match


def test_private_words_come_only_from_the_local_denylist(repo, deny):
    put(repo, {"a.md": FAKES["private-word"] + "\n"})
    assert run(repo, "--no-history")[0] == 0  # the public script knows no private word
    code, out = run(repo, "--no-history", deny=deny)
    assert code == 1 and "a.md:1: private-word" in out and WORD not in out and "3 private pattern(s)" in out
    (repo / "scripts").mkdir()
    (repo / "scripts" / "release_denylist.local.txt").write_text(WORD + "\n", encoding="utf-8")  # the default location, without the env var
    code, out = run(repo, "--no-history")
    assert "a.md:1: private-word" in out and "scripts/release_denylist.local.txt: forbidden-path" in out  # not gitignored here: flagged
    deny.write_text("(unclosed\n", encoding="utf-8")
    code, out = run(repo, "--no-history", deny=deny)
    assert code == 2 and "deny.txt:1: bad regex" in out


def test_placeholders_and_safe_look_alikes_pass(repo):
    text = "AKASH" + "_API_KEY=<your key>\nAKASH" + "_API_KEY=${AKASH" + "_API_KEY}\nC:\\Users\\<name>\\x /home/dev/x $HOME/x\nssh-key" + "gen -t ed25519\n"
    put(repo, {"docs/x.md": text})
    code, out = run(repo, "--no-history")
    assert code == 0, out


@pytest.mark.parametrize(
    "name",
    [
        "secrets.env",
        "keys/access",
        "a.pem",
        "id_ed25519",
        "b.tar.gz",
        "state.json",
        ".env",
        "prod.env",
        "sub/known_hosts",
        "x.log",
        "run.jsonl",
        ".claude/s.json",
        "pkg/build/x.txt",
        "e.egg-info/PKG-INFO",
        "a.gpg",
        "c/__pycache__/x.txt",
    ],
)
def test_forbidden_paths(repo, name):
    put(repo, {name: "harmless\n"})
    code, out = run(repo, "--no-history")
    assert code == 1 and f"{name}: forbidden-path" in out, out


def test_env_example_is_allowed(repo):
    put(repo, {".env.example": "A=b\n", "config.example.toml": "a = 1\n"})
    assert run(repo, "--no-history")[0] == 0


def test_untracked_is_checked_but_gitignored_is_not(repo):
    put(repo, {".gitignore": "ignored.txt\n", "ignored.txt": FAKES["github-token"], "new.txt": FAKES["github-token"]})
    code, out = run(repo, "--no-history")
    assert "new.txt:1: github-token" in out and "ignored.txt" not in out


def test_missing_required_files(repo):
    (repo / "SECURITY.md").unlink()
    code, out = run(repo, "--no-history")
    assert code == 1 and "SECURITY.md: missing-file" in out


def test_owner_placeholder_is_a_warning_and_strict_fails(repo):
    put(repo, {"README.md": "see github.com/" + "OWN" + "ER/clouddeck\n"})
    code, out = run(repo, "--no-history")
    assert code == 0 and "WARNING README.md:1: owner-placeholder" in out
    assert run(repo, "--no-history", "--strict")[0] == 1


def test_user_placeholders_pass_strict_but_unfilled_release_notes_do_not(repo, deny):
    own = "OWN" + "ER"
    put(repo, {"README.md": f'state_repo = "{own}/your-private-state-repo"\ngithub = "{own}/my-project"\nimage ghcr.io/{own}/ddc-base:cpu-1\n'})
    assert run(repo, "--no-history", "--strict", deny=deny)[0] == 0
    put(repo, {"docs/x.md": "Linux: " + "TBD" + "-LIVE\n"})
    code, out = run(repo, "--no-history", "--strict", deny=deny)
    assert code == 1 and "WARNING docs/x.md:1: unfilled-placeholder" in out


def test_strict_refuses_to_pass_without_private_patterns(repo, tmp_path):
    code, out = run(repo, "--no-history", "--strict")  # no local denylist, no env var: the private-word scan did not run
    assert code == 1 and "needs private denylist patterns" in out
    assert run(repo, "--no-history", "--strict", "--allow-no-denylist")[0] == 0
    code, out = run(repo, "--no-history", deny=tmp_path / "typo.txt")  # a mistyped env path is an error, not an empty list
    assert code == 2 and "not a file" in out


def test_a_comment_in_the_allowlist_cannot_hide_a_private_word(repo, deny):
    entry = f'[[allow]]\nrule = "private-word"\npath = "x"\ncontains = "{WORD}-dev-cloud" # {WORD} "\nreason = "legacy dir"\n'
    put(repo, {"scripts/release_allowlist.toml": entry})
    code, out = run(repo, "--no-history", deny=deny)
    assert code == 1 and "scripts/release_allowlist.toml:4: private-word" in out


def test_allowlist_needs_reason_and_is_exact(repo, tmp_path, deny):
    legacy = "~/.config/" + WORD + "-dev-cloud"
    put(repo, {"a.md": legacy + "\n", "b.md": legacy + " and " + FAKES["github-token"] + "\n", "c.md": legacy + "-state\n"})
    al = tmp_path / "al.toml"
    al.write_text(f'[[allow]]\nrule = "private-word"\ncontains = "{legacy}"\nreason = "legacy dir"\n')
    code, out = run(repo, "--no-history", "--allowlist", str(al), deny=deny)
    assert code == 1 and "a.md" not in out and "b.md:1: github-token" in out
    assert "c.md:1: private-word" in out  # the longer denylist entry still matches inside the allowed legacy string
    al.write_text(f'[[allow]]\nrule = "private-word"\ncontains = "{legacy}"\n')
    code, out = run(repo, "--no-history", "--allowlist", str(al), deny=deny)
    assert code == 2 and "reason" in out


def test_allowlist_file_is_scanned_except_its_contains_values(repo, deny):
    entry = f'[[allow]]\nrule = "private-word"\npath = "x"\ncontains = "{WORD}-dev-cloud"\nreason = "legacy dir"\n'
    put(repo, {"scripts/release_allowlist.toml": entry})
    assert run(repo, "--no-history", deny=deny)[0] == 0
    put(repo, {"scripts/release_allowlist.toml": entry.replace("legacy dir", "the " + WORD + " name")})
    code, out = run(repo, "--no-history", deny=deny)
    assert code == 1 and "scripts/release_allowlist.toml:5: private-word" in out


@pytest.mark.parametrize("rule", ["github-token", "private-word"])
def test_secret_removed_from_tree_is_still_found_in_history(repo, deny, rule):
    put(repo, {"old.txt": FAKES[rule] + "\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "oops")
    git(repo, "rm", "-q", "old.txt")
    git(repo, "commit", "-qm", "remove")
    assert run(repo, "--no-history", deny=deny)[0] == 0
    code, out = run(repo, deny=deny)
    assert code == 1 and f"old.txt:1: {rule}" in out and "history:" in out
    assert FAKES[rule] not in out


def test_other_branch_and_forbidden_path_in_history(repo):
    git(repo, "checkout", "-q", "-b", "side")
    put(repo, {"secrets.env": "x\n", "n.txt": FAKES["private-key"] + "\n"})
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "side")
    git(repo, "checkout", "-q", "-")  # back on the first branch: the working tree is clean, only the other branch holds the leaks
    code, out = run(repo)
    assert code == 1 and "history:secrets.env: forbidden-path" in out and "n.txt:1: private-key" in out
    assert run(repo, "--no-history")[0] == 0


def test_commit_message_leak_is_error_and_identity_is_warning(repo, deny):
    git(repo, "commit", "-q", "--allow-empty", "-m", "token " + FAKES["github-token"])
    git(repo, "commit", "-q", "--allow-empty", "-m", "see " + FAKES["session-link"])
    git(repo, "-c", "user.name=" + WORD, "commit", "-q", "--allow-empty", "-m", "fine")
    code, out = run(repo, deny=deny)
    assert code == 1 and "message:1: github-token" in out and "message:1: session-link" in out and "author/committer: private-word" in out
    assert FAKES["github-token"] not in out


def test_not_a_git_repo_still_scans_the_tree(tmp_path, deny):
    put(tmp_path / "t", {n: "ok\n" for n in REQUIRED} | {"x.txt": "dseq 4242" + "4242424"})
    code, out = run(tmp_path / "t", deny=deny)
    assert code == 1 and "x.txt:1: private-word" in out and "history scan skipped" in out


def test_script_and_this_test_do_not_match_their_own_rules():
    for f in (SCRIPT, Path(__file__)):
        assert rc.scan_text(f.name, f.read_bytes()) == [], f.name


def test_check_dist_flags_missing_data_and_stray_files(tmp_path):
    spec = importlib.util.spec_from_file_location("check_dist", SCRIPT.with_name("check_dist.py"))
    cd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cd)
    whl = tmp_path / "x-1-py3-none-any.whl"
    members = [*cd.WHEEL_NEED, "x-1.dist-info/entry_points.txt", "x-1.dist-info/licenses/LICENSE"]
    with zipfile.ZipFile(whl, "w") as z:
        for m in members:
            z.writestr(m, "x")
    assert cd.check(whl) == []
    with zipfile.ZipFile(whl, "w") as z:
        for m in members[1:] + ["ddc.py", "tests/test_x.py"]:
            z.writestr(m, "x")
    problems = cd.check(whl)
    assert f"missing {members[0]}" in problems and "unexpected ddc.py" in problems and "unexpected tests/test_x.py" in problems
