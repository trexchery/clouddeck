"""`clouddeck` command line: the TUI (default), plus scriptable status / doctor / install / remote-access commands."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

from . import __version__

PLACEHOLDER_REPO = re.compile(r"OWNER|your-private-state-repo")   # what the docs and config.example.toml use; `ddc install` refuses it too
GIT_INSTALL = 'pipx install "git+https://github.com/trexchery/clouddeck@claude-skill"'   # private repo, no PyPI release: collaborators run `gh auth login` + `gh auth setup-git` first
WARNING_FIXES = [   # (start of the controller's warning, what to do about it)
    ("unsafe_disable_limits", "Fix: remove `unsafe_disable_limits` from config.toml"),
    ("budget_guard = false", "Fix: set budget_guard = true in config.toml"),
    ("state_repo is empty", "Fix: set state_repo = \"OWNER/your-private-state-repo\" (your own private repo), then run `ddc install`"),
    ("image is a mutable tag", "Fix: set image = \"...@sha256:<digest>\" in config.toml"),
    ("push_claude_credentials", "Fix: set push_claude_credentials = false unless you need Claude Code logged in on the box"),
]


def _provider(name: str | None):
    from . import providers
    return providers.get(name or os.environ.get("CLOUDDECK_PROVIDER") or "akash")


def _need_config(a: argparse.Namespace) -> None:
    """Commands that can spend need a real config.  The simulated demo provider does not: it touches nothing."""
    from .engine import ddc
    from .providers.base import ProviderError
    if ddc.CONFIG_ERROR and (a.provider or os.environ.get("CLOUDDECK_PROVIDER") or "akash") != "demo":
        raise ProviderError(f"{ddc.CONFIG_ERROR}\n  fix: run `ddc init` (creates a config with safe default limits), or try the simulated provider: clouddeck -p demo")


def cmd_tui(a: argparse.Namespace) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("CloudDeck needs an interactive terminal. For scripts use: clouddeck status --json", file=sys.stderr)
        return 2
    _need_config(a)
    try:
        from .tui.app import CloudDeckApp
    except ImportError as e:
        print(f"Missing dependency ({e}). Install with: {GIT_INSTALL}", file=sys.stderr)
        return 2
    CloudDeckApp(_provider(a.provider)).run()
    return 0


def cmd_status(a: argparse.Namespace) -> int:
    _need_config(a)
    p = _provider(a.provider)
    insts = p.instances(True)
    s = p.spend()
    if a.json:
        print(json.dumps({"provider": p.id, "balance": s.balance, "burn_usd_hr": s.burn_hr, "today_usd": s.today, "closed_total_usd": s.closed_total,
                          "instances": [{k: v for k, v in i.__dict__.items() if k != "meta"} | {"uptime_h": i.uptime_h} for i in insts]}, indent=1, default=str))
        return 0
    bal = f"${s.balance:.2f}" if s.balance is not None else "unknown"
    print(f"{p.label}: balance {bal} | burn ${s.burn_hr:.3f}/h | today ${s.today:.2f} | closed total ${s.closed_total:.2f} | budget guard {'ON' if s.budget_guard else 'off'}")
    if not insts:
        print("No instances running (nothing is billing).")
    for i in insts:
        tag = "" if i.managed else "  (external, read-only)"
        print(f"  {i.role:8} {i.state:12} {i.cpu:>3} vCPU ${i.usd_hr:.3f}/h up {i.uptime_h:4.1f}h spent ${i.spent_usd:.3f}  {i.name}{tag}")
    return 0


def cmd_providers(a: argparse.Namespace) -> int:
    from . import providers
    for name, cls in sorted(providers.available().items()):
        print(f"{name:10} {cls.label:22} {' '.join(sorted(cls.capabilities))}")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    """Everything CloudDeck needs, checked read-only.  Never prints a secret."""
    from .engine import ddc
    bad = 0

    def line(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal bad
        bad += ok is False
        print(f"  {'PASS' if ok else 'FAIL' if ok is False else 'WARN'}  {what}" + (f"  — {detail}" if detail else ""))

    print(f"CloudDeck {__version__}")
    line(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]} (3.11+ needed)")
    try:
        import textual
        line(True, f"Textual {textual.__version__}")
    except ImportError:
        line(False, "Textual not installed", GIT_INSTALL)
    demo = (a.provider or os.environ.get("CLOUDDECK_PROVIDER") or "akash") == "demo"
    if ddc.CONFIG_ERROR:
        if demo and ddc.CONFIG_FILE is None:   # the simulated provider needs no config; every real provider does
            line(None, "config: none found", "optional for the demo provider; `ddc init` creates one with safe default limits")
        else:
            line(False, "config", f"{ddc.CONFIG_ERROR}. Fix: run `ddc init` (or set DDC_CONFIG to your config.toml)")
    else:
        line(True, f"config: {ddc.CONFIG_FILE}")
    if not ddc.CONFIG_ERROR:   # without a config there are no limits to report (the controller's defaults are not what a real run would use)
        for problem in ddc.config_problems():   # a problem here is refused by the controller too: the real providers cannot run until it is fixed
            line(None if demo else False, f"config problem: {problem}", f"Fix: edit {ddc.CONFIG_FILE}")
        if ddc.CFG.get("budget_guard", True) is not False:
            line(True, f"budget_guard = {ddc.CFG.get('budget_guard', True)}",
                 f"ON: budget ${ddc.CFG.get('budget_usd')}, reserve ${ddc.CFG.get('reserve_usd')}, at most {ddc.CFG.get('max_total_leases')} leases / {ddc.CFG.get('max_workers')} workers")
        if ddc.state_repo():
            line(None if PLACEHOLDER_REPO.search(ddc.state_repo()) else True, f"state_repo = {ddc.state_repo()}",
                 "this is the documentation placeholder: set your own private repo, or empty it to disable checkpoints" if PLACEHOLDER_REPO.search(ddc.state_repo()) else "")
        for w in ddc.config_warnings():
            line(None, w, next((fix for start, fix in WARNING_FIXES if w.startswith(start)), ""))
    if demo:  # simulated provider: no ssh, keys, secrets or network are involved
        print("  ----  demo provider: simulated, nothing else to check")
        return 1 if bad else 0
    line(shutil.which(ddc.SSH) is not None or Path(ddc.SSH).exists(), f"ssh client: {ddc.SSH}")
    cp = bool(ddc.state_repo())  # without checkpoints (jobs-only setup) gpg and the backup passphrase are optional
    line(shutil.which(ddc.GPG) is not None or Path(ddc.GPG).exists() or (None if not cp else False), f"gpg (checkpoint verification): {ddc.GPG}")
    secrets = ddc.load_secrets()
    line("AKASH_API_KEY" in secrets, f"AKASH_API_KEY present in {ddc.SECRETS_DIR / 'secrets.env'}")
    line("DDC_BACKUP_PASSPHRASE" in secrets or (None if not cp else False), "DDC_BACKUP_PASSPHRASE present (needed to decrypt checkpoints)")
    line((ddc.KEYS / "access").exists(), f"ssh access key in {ddc.KEYS}")
    try:
        n = len(ddc.public_get("/v1/providers"))
        line(True, f"Akash Console API reachable ({n} providers listed)")
    except Exception as e:
        line(False, "Akash Console API unreachable", str(e)[:100])
    if "AKASH_API_KEY" in secrets:
        try:
            line(True, f"API key accepted; balance ${ddc.balance_usd():.2f}")
        except Exception as e:
            line(False, "API key rejected or API error", str(e)[:100])
    live: list[dict] | None = None
    try:
        live = [d for d in ddc.load_state()["deps"].values() if d.get("status") != "closed"]
        line(True, f"{len(live)} lease(s) recorded as running in {ddc.STATE_FILE}")
    except Exception as e:   # a damaged state file fails closed: no lease is created until it reads cleanly
        hint = "" if "reconcile" in str(e) else ". Fix: restore it from a backup (state.json.bak) or run `ddc reconcile`; leases may still be running: check `ddc status`"
        line(False, f"state file {ddc.STATE_FILE}", f"{str(e)[:700]}{hint}")
    try:
        watchdog = ddc.watchdog_installed()
    except Exception:
        watchdog = None
    if watchdog:
        line(True, "watchdog installed", "closes idle and over-budget boxes even when CloudDeck is not open")
    elif watchdog is None:
        line(None, "watchdog: cannot tell whether it is installed on this system", "run `ddc watchdog --once` every 5 minutes yourself if `ddc install` does not support your scheduler")
    else:   # nothing else closes an idle box before the platform's runtime limit: that is a FAIL once something is running
        line(None if live == [] else False, "watchdog is NOT installed", "idle and over-budget boxes are not closed automatically (a watchdog registered for another config does not count). Fix: run "
                                         f"`ddc install` (Windows, replacing a pre-release {ddc.LEGACY_TASK} task: `ddc install --migrate`)")
    tokens = ddc.STATE_DIR / "clouddeck-tokens.json"
    if os.name != "nt" and tokens.exists() and tokens.stat().st_mode & 0o077:
        line(None, f"{tokens} is readable by other users", f"chmod 600 {tokens}")
    return 1 if bad else 0


def cmd_install(a: argparse.Namespace) -> int:
    """Create a `clouddeck` launcher next to the existing ddc / claude-cloud shims.  Existing shims are never touched."""
    from .engine import ddc
    py = sys.executable
    root = ddc.CHECKOUT  # a plain checkout needs PYTHONPATH; an installed package does not
    if os.name == "nt":
        target = Path.home() / "bin" / "clouddeck.cmd"
        body = "@echo off\r\n" + (f'set "PYTHONPATH={root};%PYTHONPATH%"\r\n' if root else "") + f'"{py}" -m clouddeck %*\r\n'
    else:
        target = Path.home() / ".local" / "bin" / "clouddeck"
        body = "#!/bin/sh\n" + (f'PYTHONPATH="{root}${{PYTHONPATH:+:$PYTHONPATH}}" ' if root else "") + f'exec "{py}" -m clouddeck "$@"\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8", newline="")
    if os.name != "nt":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {target}")
    on_path = str(target.parent).lower() in (os.environ.get("PATH", "").lower().split(os.pathsep))
    if not on_path:
        print(f"note: {target.parent} is not on PATH in this shell; open a new terminal or add it.")
    print("backward compatible: `ddc` and `claude-cloud` are unchanged.")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    from . import remote
    if not (a.list_tokens or a.revoke or a.new_token):   # token housekeeping needs no config; serving (and scaling for the box) does
        _need_config(a)
    return remote.serve_cli(a)


def cmd_remote(a: argparse.Namespace) -> int:
    from . import remote
    return remote.client_cli(a)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clouddeck", description="Keyboard-driven terminal UI for on-demand cloud compute (Akash first).")
    p.add_argument("--version", action="version", version=f"clouddeck {__version__}")
    p.add_argument("-p", "--provider", help="akash (default) | demo (simulated, safe) | any plugin; or set CLOUDDECK_PROVIDER")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("tui", help="open the TUI (default)")
    s = sub.add_parser("status", help="print instances and spend, then exit")
    s.add_argument("--json", action="store_true")
    sub.add_parser("providers", help="list providers and their capabilities")
    sub.add_parser("doctor", help="check the environment (read-only)")
    sub.add_parser("install", help="create the `clouddeck` launcher (existing ddc/claude-cloud shims are untouched)")
    s = sub.add_parser("serve", help="controller API on 127.0.0.1 for remote terminals (token-authenticated)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--new-token", metavar="LABEL", help="create a token (printed once) and exit")
    s.add_argument("--scope", choices=["read", "fleet", "admin"], default="read",
                   help="read = status only; fleet = also scale (within max_workers and the budget guard), dispatch to workers, checkpoint; admin = also run commands on the dev box")
    s.add_argument("--expires-hours", type=float, default=None, metavar="H",
                   help="token lifetime in hours (default 24; with --tunnel: the dev box's runtime limit + 1; 0 = never)")
    s.add_argument("--revoke", metavar="LABEL", help="revoke a token and exit")
    s.add_argument("--list-tokens", action="store_true")
    s.add_argument("--tunnel", action="store_true", help="push a token + clouddeck-remote to the running dev box and keep an SSH reverse tunnel to it open")
    s = sub.add_parser("remote", help="talk to a controller from a remote terminal (e.g. the Akash dev box)")
    s.add_argument("action", choices=["status", "instances", "scale", "dispatch", "checkpoint"])
    s.add_argument("arg", nargs="*")
    s.add_argument("--url", help="default $CLOUDDECK_URL or http://127.0.0.1:8765")
    s.add_argument("--json", action="store_true")
    for sp in sub.choices.values():  # also accept `clouddeck doctor -p demo` (SUPPRESS keeps the value given before the subcommand)
        sp.add_argument("-p", "--provider", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    fn = {None: cmd_tui, "tui": cmd_tui, "status": cmd_status, "providers": cmd_providers, "doctor": cmd_doctor, "install": cmd_install,
          "serve": cmd_serve, "remote": cmd_remote}[a.cmd]
    try:
        return fn(a)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        from .engine import DdcError
        from .providers.base import ProviderError
        if isinstance(e, (ProviderError, DdcError)):   # includes subclasses such as a damaged-state error
            print(f"error: {e}", file=sys.stderr)
            return 1
        if os.environ.get("CLOUDDECK_DEBUG"):
            raise
        print(f"error: {type(e).__name__}: {e}\n  (unexpected; set CLOUDDECK_DEBUG=1 for the full traceback and please report it)", file=sys.stderr)
        return 1


def claude_cloud(argv: list[str] | None = None) -> int:
    """`claude-cloud [window]` on Linux/macOS installs: same as `ddc attach`."""
    from .engine import ddc
    return ddc.main(["attach", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
