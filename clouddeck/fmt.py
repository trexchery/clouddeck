"""Small display helpers shared by the TUI and the CLI."""
from __future__ import annotations

import time


def money(x: float | None, places: int = 2) -> str:
    return "—" if x is None else f"${x:,.{places}f}"


def rate(x: float | None) -> str:
    return "—" if x is None else f"${x:.3f}/h"


def hours(h: float | None) -> str:
    if h is None:
        return "—"
    m = int(round(h * 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def ago(t: float | None, now: float | None = None) -> str:
    if not t:
        return "never"
    s = max(0, int((now or time.time()) - t))
    return f"{s}s ago" if s < 90 else f"{s // 60}m ago" if s < 5400 else f"{s // 3600}h ago"


def gb(x: float | None) -> str:
    return "—" if x is None else f"{x:.0f}G" if x >= 10 else f"{x:.1f}G"


def bar(frac: float | None, width: int = 10) -> str:
    """Unicode bar, e.g. ██████░░░░."""
    if frac is None:
        return "·" * width
    frac = min(1.0, max(0.0, frac))
    full = round(frac * width)
    return "█" * full + "░" * (width - full)


def pct(frac: float | None) -> str:
    return "—" if frac is None else f"{frac * 100:.0f}%"
