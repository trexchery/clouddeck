"""Loads the proven ddc controller (clouddeck.controller) as a library.

CloudDeck never re-implements Akash logic: leases, bids, SSH, checkpoints and the safety rules
(only state-recorded `ddc-*` leases can be closed) all stay in the controller, so the `ddc` CLI and the TUI
always agree on repos, profiles and limits (same config lookup, same state directory).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from . import controller as ddc

DdcError = ddc.DdcError


def set_log_hook(hook: Callable[[str], None]) -> Callable[[str], None]:
    """Route ddc's log lines to the UI (printing would corrupt the screen) and still keep ddc.log.  Returns the previous `ddc.log` so the UI
    can put it back with `restore_log` when it closes.  A failing hook never breaks a lease operation."""
    previous = ddc.log

    def log(msg: str) -> None:
        now = dt.datetime.now()
        ddc.STATE_DIR.mkdir(parents=True, exist_ok=True)
        with ddc.LOG.open("a", encoding="utf-8") as f:
            f.write(now.isoformat(timespec="seconds") + " " + msg + "\n")
        try:
            hook(f"[{now:%H:%M:%S}] {msg}")
        except Exception:   # a UI that is going away must not take a lease operation down with it
            pass

    ddc.log = log
    return previous


def restore_log(previous: Callable[[str], None]) -> None:
    ddc.log = previous
