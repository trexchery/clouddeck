#!/usr/bin/env python3
"""Release hygiene gate (stdlib only, Python >= 3.11): fails when a private identifier, secret or forbidden file is in the repo.

    python scripts/release_check.py             # working tree (tracked + untracked-not-ignored) AND the full git history
    python scripts/release_check.py --no-history
    python scripts/release_check.py --strict    # placeholder and identity warnings also fail (use before publishing)

Exit: 0 ok, 1 findings, 2 usage/config error.  Findings print as `location: rule-id (what it means)`; the matched text is NEVER printed,
so the output is safe to paste into a public CI log.  Legit legacy-compat strings go into scripts/release_allowlist.toml (every entry needs a
`reason`).  Patterns are built from fragments so this file does not match its own rules.

This public script only knows generic rules.  Private words (names, project names, deployment ids) live in a local, gitignored denylist:
scripts/release_denylist.local.txt, or the file named by $CLOUDDECK_RELEASE_DENYLIST.  One case-insensitive regex per line, `#` comments;
each line is its own `private-word` rule, so a longer entry still matches inside text that the allowlist permits for a shorter one.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # --root overrides (tests, other checkouts)
ALLOWLIST = ROOT / "scripts" / "release_allowlist.toml"  # --allowlist overrides; scanned too, except inside its own `contains = "..."` values
DENYLIST_ENV = "CLOUDDECK_RELEASE_DENYLIST"
DENYLIST: Path | None = None  # set by main()
MAX_BYTES = 8_000_000
REQUIRED = ["LICENSE", "README.md", "SECURITY.md", "CONTRIBUTING.md", ".env.example", ".gitignore", "docs/ARCHITECTURE.md"]


def die(msg: str) -> None:
    print(f"release_check: {msg}", file=sys.stderr)
    raise SystemExit(2)


def _j(*parts: str) -> str:
    return "".join(parts)


# ---- content rules: (id, regex, meaning).  Regexes are matched per line; `i` = case-insensitive ----
_PLACEHOLDER_USERS = r"(?:<[^>]*>|%[^%]*%|\$\{?\w*\}?|\{[^}]*\}|user|username|you|yourname|me|name|example|dev|runner|public|default|all users|\.\.\.)"
_USER_PATHS = _j(r"(?:\b[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)", r"(?!", _PLACEHOLDER_USERS, r"(?:[\\/\s'\"`:;,)]|$))", r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PRIVATE_KEY = _j("-----BEGIN (?:[A-Z]+ )*PRIV", "ATE KEY(?: BLOCK)?-----")
_ENV_KEY = _j("AKASH", "_API_", "KEY")

RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("user-path", re.compile(_USER_PATHS, re.I), "absolute path containing a real user name"),
    ("session-link", re.compile(_j(r"claude\.ai/code/", "session_"), re.I), "link to a private Claude session"),
    ("akash-address", re.compile(_j(r"\bakash1", "[0-9a-z]{38}", r"\b")), "Akash account address"),
    ("private-key", re.compile(_PRIVATE_KEY), "private key material"),
    ("github-token", re.compile(_j(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}", "|", "github_", "pat_[A-Za-z0-9_]{30,})")), "GitHub token"),
    ("api-token", re.compile(_j(r"\b(?:sk-ant-[A-Za-z0-9_-]{20,}", "|", "AK", "IA[0-9A-Z]{16}", "|", r"xox[abprs]-[A-Za-z0-9-]{10,}", "|", r"glpat-[A-Za-z0-9_-]{20,})")), "API token"),
    ("akash-api-key", re.compile(_j(_ENV_KEY, r"""\s*[=:]\s*["']?([A-Za-z0-9._-]{16,})""")), "Akash API key assignment with a real-looking value"),
]
PRIVATE: list[tuple[str, re.Pattern[str], str]] = []  # filled from the local denylist by load_denylist()
WARN_RULES = [  # fail only with --strict: unfilled release placeholders (scripts/ is skipped, it names them)
    ("owner-placeholder", re.compile(_j(r"github\.com/OWN", r"ER/clouddeck\b")), "project URL placeholder to replace before publishing"),
    ("unfilled-placeholder", re.compile(_j("TBD", "-LIVE")), "release-manager placeholder to fill in before publishing"),
]
_FAKE = re.compile(r"your|xxx|example|placeholder|changeme|redacted|dummy|fake|sample|test|\.\.\.|^(?:os\.environ|environ)", re.I)

# ---- path rules: matched against every path component / the basename ----
_BAD_DIRS = {"keys", "checkpoints", "build", "dist", "__pycache__", ".claude", "sessions", "transcripts", "node_modules", ".venv", "venv"}
_BAD_NAMES = [
    "secrets.env",
    "state.json",
    "known_hosts*",
    "claude-credentials.json",
    ".npmrc",
    ".netrc",
    "config.toml",
    "clouddeck.toml",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.gpg",
    "*.gpg.*",
    "*.tar",
    "*.tar.*",
    "*.tgz",
    "*.log",
    "*.jsonl",
    "*.whl",
    "*.egg-info",
    "*.py[cod]",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.env",
    "release_denylist.local*",  # the private denylist: gitignored, never committed
    ".env",
    ".env.*",
]
_OK_NAMES = {".env.example", "config.example.toml"}


def load_denylist(path: Path | None) -> None:
    PRIVATE.clear()
    if not path or not path.is_file():
        return
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if (pat := raw.strip()) and not pat.startswith("#"):
            try:
                PRIVATE.append(("private-word", re.compile(pat, re.I), "private/project-internal identifier"))
            except re.error as e:
                die(f"{path.name}:{n}: bad regex ({e})")


def bad_path(path: str) -> str | None:
    parts = path.split("/")
    for d in parts[:-1]:
        if d in _BAD_DIRS or d.endswith(".egg-info"):
            return f"forbidden directory '{d}/'"
    name = parts[-1]
    if name in _OK_NAMES or name.endswith(".env.example"):
        return None
    for pat in _BAD_NAMES:
        if fnmatch.fnmatchcase(name, pat):
            return f"forbidden file name pattern '{pat}'"
    return None


@dataclass(frozen=True)
class Finding:
    loc: str  # "path:line" / "history:<blob>:path:line"
    rule: str
    what: str
    path: str
    line: str = ""  # kept for the allowlist only, never printed
    span: tuple[int, int] = (0, 0)


class Allow:
    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.used = [False] * len(entries)

    @classmethod
    def load(cls, path: Path) -> Allow:
        if not path.exists():
            return cls([])
        try:
            entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
        except tomllib.TOMLDecodeError as e:
            die(f"{path.name}: {e}")
        for i, e in enumerate(entries):
            if not str(e.get("reason", "")).strip():
                die(f"{path.name}: allow entry #{i + 1} has no 'reason'")
            if not e.get("rule") or not (e.get("path") or e.get("contains")):
                die(f"{path.name}: allow entry #{i + 1} needs 'rule' and 'path' and/or 'contains'")
        return cls(entries)

    @staticmethod
    def _covers_span(needle: str, f: Finding) -> bool:
        """`contains` must enclose the matched text itself, so an allowed legacy string cannot hide a second leak on the same line."""
        low, n, at = f.line.lower(), needle.lower(), -1
        while (at := low.find(n, at + 1)) >= 0:
            if at <= f.span[0] and f.span[1] <= at + len(n):
                return True
        return False

    def covers(self, f: Finding) -> bool:
        for i, e in enumerate(self.entries):
            if e["rule"] not in ("*", f.rule):
                continue
            if e.get("path") and not fnmatch.fnmatchcase(f.path, e["path"]):
                continue
            if e.get("contains") and not self._covers_span(e["contains"], f):
                continue
            self.used[i] = True
            return True
        return False


def scan_text(path: str, data: bytes, loc_prefix: str = "") -> list[Finding]:
    if b"\0" in data[:8192] or len(data) > MAX_BYTES:
        return []
    out = []
    for n, raw in enumerate(data.split(b"\n"), 1):
        line = raw.decode("utf-8", "replace")
        for rid, rx, what in RULES + PRIVATE:
            for m in rx.finditer(line):
                if rid == "akash-api-key" and _FAKE.search(m.group(1)):
                    continue
                out.append(Finding(f"{loc_prefix}{path}:{n}", rid, what, path, line, m.span()))
    return out


def git(*args: str, check: bool = True) -> bytes:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, capture_output=True)
    if check and r.returncode:
        die(f"git {args[0]} failed: {r.stderr.decode(errors='replace').strip()[:200]}")
    return r.stdout


def is_git() -> bool:
    r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=ROOT, capture_output=True)
    return r.returncode == 0 and Path(r.stdout.decode(errors="replace").strip()).resolve() == ROOT.resolve()


def worktree_paths() -> list[str]:
    if is_git():
        out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return sorted({p for p in out.decode("utf-8", "replace").split("\0") if p and os.path.lexists(ROOT / p)})  # index entries deleted on disk are gone
    skip = {".git"}
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts))


def allow_rel() -> str | None:
    try:
        return ALLOWLIST.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None


_CONTAINS = re.compile(r"""^\s*contains\s*=\s*(["'])((?:(?!\1).)*)\1\s*(?:#.*)?$""")  # a trailing comment is not part of the value


def own_contains(f: Finding) -> bool:
    """A match in the allowlist itself is fine only inside one of its `contains` values (comments and reasons are scanned like any file)."""
    m = f.path == allow_rel() and _CONTAINS.match(f.line)
    return bool(m) and m.start(2) <= f.span[0] and f.span[1] <= m.end(2)


def check_tree(paths: list[str], allow: Allow) -> list[Finding]:
    found = []
    for p in paths:
        if why := bad_path(p):
            found.append(Finding(p, "forbidden-path", why, p))
        f = ROOT / p
        if f.is_file() and not f.is_symlink():
            found += scan_text(p, f.read_bytes())
    return uniq([f for f in found if not own_contains(f) and not allow.covers(f)])


def check_history(allow: Allow) -> tuple[list[Finding], list[Finding], int]:
    """Every blob reachable from any ref (rev-list --objects --all + cat-file --batch), every path ever committed, every commit message."""
    errors: list[Finding] = []
    warns: list[Finding] = []
    if not git("rev-list", "-n1", "--all", check=False).strip():
        return errors, warns, 0
    names = {p for p in git("log", "--all", "-m", "--no-renames", "--name-only", "-z", "--pretty=format:").decode("utf-8", "replace").split("\0") if p}
    for p in sorted(names):
        if why := bad_path(p):
            errors.append(Finding(f"history:{p}", "forbidden-path", why, p))
    objs = git("rev-list", "--objects", "--all").decode("utf-8", "replace").splitlines()
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin and proc.stdout
    n_blobs = 0
    for entry in objs:
        oid, _, path = entry.partition(" ")
        proc.stdin.write(oid.encode() + b"\n")
        proc.stdin.flush()
        _, otype, size = proc.stdout.readline().split()
        data = proc.stdout.read(int(size))
        proc.stdout.read(1)
        if otype != b"blob" or not path:
            continue
        n_blobs += 1
        errors += [f for f in scan_text(path, data, f"history:{oid[:10]}:") if not own_contains(f)]
    proc.stdin.close()
    proc.wait()
    # commit messages: leaks are errors; author/committer identities are only warnings (the owner may publish under a real name on purpose)
    for rec in git("log", "--all", "--pretty=format:%h%x1f%an <%ae>%x1f%cn <%ce>%x1f%B%x1e").decode("utf-8", "replace").split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 4:
            continue
        h, who, body = parts[0].strip(), parts[1] + "\n" + parts[2], parts[3]
        errors += [Finding(f"history:commit {h} message:{f.loc.rsplit(':', 1)[1]}", f.rule, f.what, "<commit-message>", f.line, f.span) for f in scan_text("", body.encode())]
        warns += [Finding(f"history:commit {h} author/committer", f.rule, f.what, "<commit-identity>", f.line, f.span) for f in scan_text("", who.encode())]
    return uniq([f for f in errors if not allow.covers(f)]), uniq([f for f in warns if not allow.covers(f)]), n_blobs


def uniq(fs: list[Finding]) -> list[Finding]:
    return list({(f.loc, f.rule): f for f in fs}.values())


def placeholder_warnings(paths: list[str]) -> list[Finding]:
    out = []
    for p in paths:
        f = ROOT / p
        if f.is_file() and not f.is_symlink() and not p.startswith("scripts/"):
            data = f.read_bytes()
            for rid, rx, what in WARN_RULES:
                out += [Finding(x.loc, rid, what, p) for x in scan_text_rx(p, data, rx)]
    return out


def scan_text_rx(path: str, data: bytes, rx: re.Pattern[str]) -> list[Finding]:
    if b"\0" in data[:8192] or len(data) > MAX_BYTES:
        return []
    return [Finding(f"{path}:{n}", "", "", path) for n, raw in enumerate(data.split(b"\n"), 1) if rx.search(raw.decode("utf-8", "replace"))]


def main(argv: list[str] | None = None) -> int:
    global ROOT, ALLOWLIST, DENYLIST
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=ROOT, help="repository to check (default: the checkout this script lives in)")
    ap.add_argument("--allowlist", type=Path, help="allowlist TOML (default: <root>/scripts/release_allowlist.toml)")
    ap.add_argument("--no-history", action="store_true", help="skip the git history scan")
    ap.add_argument("--strict", action="store_true", help="warnings (unfilled placeholders, commit identities) also fail")
    ap.add_argument("--allow-no-denylist", action="store_true", help="with --strict: pass even when no private denylist pattern is loaded")
    args = ap.parse_args(argv)
    ROOT = args.root.resolve()
    ALLOWLIST = args.allowlist.resolve() if args.allowlist else ROOT / "scripts" / "release_allowlist.toml"
    DENYLIST = Path(os.environ[DENYLIST_ENV]).resolve() if os.environ.get(DENYLIST_ENV) else ROOT / "scripts" / "release_denylist.local.txt"
    if os.environ.get(DENYLIST_ENV) and not DENYLIST.is_file():  # a mistyped path must not silently turn the private-word scan off
        die(f"{DENYLIST_ENV}={DENYLIST}: not a file")
    load_denylist(DENYLIST)
    allow = Allow.load(ALLOWLIST)
    paths = worktree_paths()
    errors = check_tree(paths, allow)
    warns = placeholder_warnings(paths)
    errors += [Finding(r, "missing-file", "required file is missing", r) for r in REQUIRED if r not in set(paths)]
    blobs = 0
    if not args.no_history:
        if is_git():
            e, w, blobs = check_history(allow)
            errors += e
            warns += w
        else:
            print("note: not a git repository, history scan skipped")
    for f in errors:
        print(f"ERROR   {f.loc}: {f.rule} ({f.what})")
    for f in warns:
        print(f"WARNING {f.loc}: {f.rule} ({f.what})")
    for e, used in zip(allow.entries, allow.used):
        if not used and not args.no_history:
            print(f"note: unused allowlist entry (rule={e['rule']}, path={e.get('path', '*')}, contains={e.get('contains', '*')!r}): remove it?")
    scope = ("tree" if args.no_history else f"tree + history ({blobs} blobs)") + f", {len(PRIVATE)} private pattern(s)"
    print(f"release_check: {len(errors)} error(s), {len(warns)} warning(s) [{scope}]")
    if args.strict and not PRIVATE and not args.allow_no_denylist:
        print(f"release_check: --strict needs private denylist patterns ({DENYLIST} or ${DENYLIST_ENV}); pass --allow-no-denylist to skip that scan")
        return 1
    return 1 if errors or (args.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(main())
