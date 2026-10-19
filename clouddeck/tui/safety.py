"""What the user sees before anything is destroyed or any money is committed.

Every terminate, close, resize and scale-down goes through `confirm_destroy`: it lists exactly what will be destroyed, what it cost so far,
what the checkpoint situation is, and only enables the button once a phrase is typed.  Every deploy, resize and scale-up shows the spend
guard's verdict (budget ceiling = price cap x hours) first and is not offered at all when the guard says no."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from rich.table import Table
from rich.text import Text

from .. import fmt
from ..engine import ddc
from ..providers.base import ROLE_LABEL, Instance, Spec, Verdict
from .widgets import ConfirmScreen, kv_table

DiscardRefused = ddc.DiscardRefused


def newest_checkpoint(app: Any) -> str:
    """Creation time of the newest checkpoint copy verified on this PC, or ''."""
    try:
        items = app.provider.checkpoints()
    except Exception:
        return ""
    return max((c.created_at for c in items), default="")


def checkpoint_status(app: Any, victims: Sequence[Instance]) -> Text:
    """Will what is about to be closed be saved?  Says so plainly, in red where the answer is no."""
    p = app.provider
    out = Text()

    def line(t: str, style: str = "") -> None:
        if out:
            out.append("\n")
        out.append(t, style=style)

    if any(v.role == "dev" for v in victims):
        newest = newest_checkpoint(app)
        if not p.has("checkpoint"):
            line("this provider has no checkpoints: everything on the dev box is lost", "bold red")
        elif not p.checkpoint_enabled():
            line("CHECKPOINTS ARE OFF (state_repo is empty): anything not pushed to GitHub is lost", "bold red")
        else:
            line("a final checkpoint is made and verified on this PC first; if that fails nothing is closed", "green")
            line("newest verified copy on this PC: " + (newest.replace("T", " ").rstrip("Z") + " UTC" if newest else "none yet"), "dim")
    if any(v.role == "worker" for v in victims):
        line("workers hold no data: nothing to save", "dim")
    if any(v.role not in ("dev", "worker") for v in victims):
        line("generic instances are NOT checkpointed: everything on them is lost", "bold red")
    return out


def guard_rows(v: Verdict) -> list[tuple[str, Any]]:
    return [("Budget ceiling", Text(f"{fmt.money(v.ceiling_usd)}  (price cap × hours × count: the most it can cost)", style="bold yellow")),
            ("Spend guard", Text(("OK  " if v.ok else "BLOCKED  ") + v.text, style="green" if v.ok else "bold red"))]


def refuse(app: Any, v: Verdict) -> None:
    """The guard said no: tell the user why instead of offering a confirmation."""
    app.log_event(f"spend guard refused: {v.text}")
    app.notify(v.text, title="Refused by the spend guard", severity="error", timeout=15)


def destroy_body(app: Any, victims: Sequence[Instance], *, replacement: Spec | None = None, verdict: Verdict | None = None,
                 notes: Sequence[str] = ()) -> Table:
    names = Text()
    for i in victims:
        if names:
            names.append("\n")
        names.append(i.name, style="bold")
        names.append(f"   {ROLE_LABEL.get(i.role, i.role).split(' (')[0]} · {i.cpu} vCPU · {fmt.rate(i.usd_hr)} · up {fmt.hours(i.uptime_h)} · spent {fmt.money(i.spent_usd, 3)}", style="dim")
    rows: list[tuple[str, Any]] = [("Destroys", names),
                                   ("Spent so far", fmt.money(sum(i.spent_usd for i in victims), 3) + "  (billing stops at " + fmt.rate(sum(i.usd_hr for i in victims)) + ")"),
                                   ("Checkpoint", checkpoint_status(app, victims))]
    if replacement:
        rows.append(("Replaced by", f"{replacement.label()} · cap {fmt.rate(replacement.max_usd_hr)} · {replacement.hours} h"))
    if verdict:
        rows += guard_rows(verdict)
    rows += [("", Text(n, style="dim")) for n in notes]
    return kv_table(rows)


def discards_workspace(app: Any, victims: Sequence[Instance]) -> bool:
    """Closing the dev box with checkpoints switched off destroys its workspace for good: the controller refuses that unless told."""
    return any(v.role == "dev" for v in victims) and app.provider.has("checkpoint") and not app.provider.checkpoint_enabled()


def confirm_destroy(app: Any, title: str, victims: Sequence[Instance], *, word: str, label: str, on_ok: Callable[[bool], None],
                    replacement: Spec | None = None, verdict: Verdict | None = None, notes: Sequence[str] = ()) -> None:
    """The single path for every destructive action: details, then a typed phrase.  `on_ok(discard_workspace)` runs only after it is typed;
    when the workspace would be destroyed unsaved the phrase is "discard workspace" instead, and `discard_workspace` is True."""
    discard = discards_workspace(app, victims)
    body = destroy_body(app, victims, replacement=replacement, verdict=verdict, notes=notes)

    def typed(ok: bool) -> None:
        if ok:
            app.retry_discard = lambda: on_ok(True)   # if the controller still refuses (e.g. state_repo was emptied meanwhile), `discard_prompt` asks again
            on_ok(discard)
    app.push_screen(ConfirmScreen(title, body, label, danger=True, type_to_confirm="discard workspace" if discard else word), typed)


def is_discard_refusal(e: BaseException) -> bool:
    """Did the controller refuse to close the dev box because its workspace cannot be saved (checkpoints off)?  The provider may have re-wrapped the error."""
    return any(isinstance(x, DiscardRefused) for x in (e, e.__cause__)) or "checkpoints are disabled" in str(e)


def discard_prompt(app: Any, error: str) -> None:
    """A close was refused because the workspace would be lost: nothing was closed.  Offer the one explicit way forward (type the words) or cancel."""
    retry, app.retry_discard = app.retry_discard, None   # one retry only: never a loop
    if not retry:
        return
    body = kv_table([("Refused", Text(error, style="bold red")),
                     ("Checkpoint", Text("CHECKPOINTS ARE OFF (state_repo is empty): anything on the dev box that is not pushed to GitHub is lost for good", style="bold red")),
                     ("Nothing closed", Text("everything is still running and billing; Cancel keeps it that way", style="green"))])
    app.push_screen(ConfirmScreen("Close WITHOUT saving the workspace?", body, "Discard workspace", danger=True, type_to_confirm="discard workspace"),
                    lambda ok: retry() if ok else None)
