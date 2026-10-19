"""Recovery tab: checkpoints, restore, reconnect, heal, idle-shutdown policy."""
from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Label, Static

from .. import fmt
from ..providers.base import CheckpointInfo
from .safety import confirm_destroy
from .widgets import Col, Field, FilterTable, FormScreen, Pane, SelectScreen, kv_table

SNOOZES = [("30 minutes", 30, "keep the dev box up this long even if idle"), ("1 hour", 60, ""), ("2 hours", 120, ""), ("4 hours", 240, ""),
           ("Clear the snooze", 0, "go back to the normal idle shutdown")]


class RecoveryPane(Pane):
    primary = "#ckpt"
    BINDINGS = [
        Binding("c", "checkpoint", "Checkpoint"), Binding("v", "verify", "Verify"), Binding("a", "reconnect", "Reconnect"), Binding("h", "heal", "Heal workers"),
        Binding("s", "restore", "Start/Restore"), Binding("z", "snooze", "Snooze idle"), Binding("t", "settings", "Idle settings", show=False),
        Binding("X", "stop_all", "Stop everything", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ckpts: list[CheckpointInfo] = []

    def compose(self) -> ComposeResult:
        yield Static(id="dev-status")
        yield Label("Checkpoints verified on this PC (encrypted copies also live in the private state repo)", classes="section")
        yield FilterTable([Col("Created (UTC)", "at"), Col("Reason", "why"), Col("Repos at checkpoint", "repos"), Col("Entries", "n", right=True)], id="ckpt",
                          empty="No verified checkpoint copies on this PC yet. c makes one (needs a running dev box).")
        with Horizontal(id="actions"):
            yield Button("Checkpoint (c)", id="b-ckpt", variant="primary")
            yield Button("Verify (v)", id="b-verify")
            yield Button("Reconnect (a)", id="b-reconnect")
            yield Button("Heal workers (h)", id="b-heal")
            yield Button("Start/Restore (s)", id="b-restore")
            yield Button("Snooze idle (z)", id="b-snooze")
            yield Button("Stop everything (X)", id="b-stop", variant="error")

    def palette(self) -> list[SystemCommand]:
        return [SystemCommand("Checkpoint now", "Encrypted checkpoint of the dev box, verified on this PC", self.action_checkpoint),
                SystemCommand("Verify newest checkpoint", "Decrypt it here and compare entry counts", self.action_verify),
                SystemCommand("Reconnect to the dev box", "Re-attach Claude Code over ssh+tmux", self.action_reconnect),
                SystemCommand("Snooze idle shutdown…", "Keep the dev box up a while longer", self.action_snooze),
                SystemCommand("Stop everything safely…", "Final verified checkpoint, then close all leases", self.action_stop_all)]

    # ---- data
    def on_show(self) -> None:
        self.refresh_view("manual")

    def refresh_view(self, kind: str) -> None:
        if not self.is_mounted or kind in ("events", "probes"):
            return
        app: Any = self.app
        if kind in ("all", "manual", "instances"):
            if app.provider.has("checkpoint"):
                app.bg(app.provider.checkpoints, self._ckpts, "Checkpoints")
        if kind in ("manual",) and app.dev and app.provider.has("snooze"):
            app.poll("idle", app.provider.idle_state, app._got_idle)
        self._status()

    def _ckpts(self, items: list[CheckpointInfo]) -> None:
        self.ckpts = items
        self.query_one("#ckpt", FilterTable).set_rows(
            [(c.created_at, [c.created_at.replace("T", " ").rstrip("Z"), c.reason, "  ".join(f"{k}@{v[:7]}" for k, v in c.repos.items()), sum(c.entries.values())])
             for c in items])
        self._status()

    def _status(self) -> None:
        app: Any = self.app
        dev, idle = app.dev, app.idle
        rows: list[tuple[str, Any]] = []
        if not dev:
            rows.append(("Dev box", Text("not running", style="yellow")))
            newest = self.ckpts[0].created_at if self.ckpts else None
            rows.append(("To restore", Text(f"press s: a new dev box restores the newest checkpoint ({newest})" if newest else "press s to start a fresh dev box", style="dim")))
        else:
            rows.append(("Dev box", Text(f"● running · up {fmt.hours(dev.uptime_h)} · platform limit in {fmt.hours(dev.remaining_h)}", style="green")))
            if idle:
                frac = idle.idle_min / idle.stop_at_min if idle.stop_at_min else 0
                left = max(0, idle.stop_at_min - idle.idle_min)
                style = "green" if frac < 0.6 else "yellow" if frac < 0.9 else "bold red"
                rows.append(("Idle timer", Text(f"{fmt.bar(frac, 20)}  idle {idle.idle_min} of {idle.stop_at_min} min → " +
                                                (f"stops in {left} min" if not idle.shutdown_requested else f"SHUTDOWN REQUESTED ({idle.shutdown_requested})"), style=style)))
                rows.append(("Active because", idle.active_reason or Text("nothing (idle)", style="dim")))
                last = idle.last_checkpoint.replace("T", " ").rstrip("Z") if idle.last_checkpoint else "none yet"
                rows.append(("Last checkpoint", Text(last + ("   CHECKPOINTS FAILING" if idle.checkpoint_failed else ""), style="bold red" if idle.checkpoint_failed else "")))
                if idle.held_until > time.time():
                    eff = getattr(app.provider, "snooze_effective", lambda: True)()
                    rows.append(("Snoozed", Text(f"until {time.strftime('%H:%M', time.localtime(idle.held_until))}" +
                                                 ("" if eff else "  — the scheduled watchdog is still the old build, so it will NOT honor this yet (merge the clouddeck branch)"),
                                                 style="cyan" if eff else "bold yellow")))
            elif app.problems.get("idle"):
                rows.append(("Idle timer", Text(app.problems["idle"][:120], style="red")))
            else:
                rows.append(("Idle timer", Text("reading…", style="dim")))
        rows.append(("Workers", f"{len(app.fleet)} running" + ("  (a lost one? Heal re-provisions workers whose container restarted)" if app.fleet else "")))
        self.query_one("#dev-status", Static).update(kv_table(rows))
        for b in ("b-ckpt", "b-verify", "b-reconnect", "b-snooze"):
            try:
                self.query_one(f"#{b}", Button).disabled = not dev and b != "b-verify"
            except Exception:
                pass
        self.query_one("#b-heal", Button).disabled = not app.fleet
        self.query_one("#b-stop", Button).disabled = not any(i.managed for i in app.instances)

    # ---- actions
    def _need_dev(self) -> bool:
        if not self.app.dev:  # type: ignore[attr-defined]
            self.app.notify("The dev box is not running. s starts / restores one.", severity="warning")  # type: ignore[attr-defined]
            return False
        return True

    def action_checkpoint(self) -> None:
        app: Any = self.app
        if not self._need_dev() or not app.provider.has("checkpoint"):
            return

        def done(c: CheckpointInfo) -> None:
            app.notify(f"{c.created_at} · {sum(c.entries.values())} entries · {len(c.repos)} repos verified on this PC", title="Checkpoint verified", timeout=8)
            self.refresh_view("manual")
        app.run_task("Checkpoint now", lambda log: app.provider.checkpoint(log), done)

    def action_verify(self) -> None:
        app: Any = self.app
        if not app.provider.has("checkpoint"):
            return
        app.run_task("Verify newest checkpoint", lambda log: app.provider.verify(log), lambda c: (app.notify(f"{c.created_at} decrypts and matches its manifest", title="Checkpoint OK"),
                                                                                                   self.refresh_view("manual")))

    @on(Button.Pressed, "#b-ckpt")
    def _b1(self) -> None:
        self.action_checkpoint()

    @on(Button.Pressed, "#b-verify")
    def _b2(self) -> None:
        self.action_verify()

    @on(Button.Pressed, "#b-reconnect")
    def action_reconnect(self) -> None:
        if self._need_dev():
            self.app.attach()  # type: ignore[attr-defined]

    @on(Button.Pressed, "#b-heal")
    def action_heal(self) -> None:
        app: Any = self.app
        if app.fleet:
            app.run_task("Heal workers", lambda log: app.provider.heal(log))

    @on(Button.Pressed, "#b-restore")
    def action_restore(self) -> None:
        app: Any = self.app
        if app.dev:
            app.notify("A dev box is already running. Resize or terminate it from Compute.", severity="warning")
            return
        app.action_tab("compute")
        pane = next((p for p in app.panes if hasattr(p, "action_deploy")), None)
        if pane:
            pane.action_deploy()

    @on(Button.Pressed, "#b-snooze")
    def action_snooze(self) -> None:
        app: Any = self.app
        if not self._need_dev() or not app.provider.has("snooze"):
            return
        app.push_screen(SelectScreen("Snooze the idle shutdown", [(n, m, d) for n, m, d in SNOOZES]), self._snoozed)

    def _snoozed(self, minutes: int | None) -> None:
        if minutes is None:
            return
        app: Any = self.app

        def done(until: float) -> None:
            app.notify("snooze cleared" if not until else f"idle shutdown snoozed until {time.strftime('%H:%M', time.localtime(until))}"
                       + ("" if getattr(app.provider, "snooze_effective", lambda: True)() else " (needs an updated watchdog: reinstall/update ddc)"),
                       severity="information" if getattr(app.provider, "snooze_effective", lambda: True)() or not until else "warning", timeout=10)
            app.poll("idle", app.provider.idle_state, app._got_idle)
        app.bg(lambda: app.provider.hold_idle(minutes), done, "Snooze idle shutdown")

    def action_settings(self) -> None:
        from ..engine import ddc
        app: Any = self.app
        form = FormScreen("Idle & checkpoint policy", [Field("idle_minutes", "Dev box idle minutes before stop", "int", ddc.CFG["idle_minutes"], minimum=5, maximum=1440),
                                                       Field("worker_idle_minutes", "Workers idle minutes before close", "int", ddc.CFG["worker_idle_minutes"], minimum=2, maximum=1440),
                                                       Field("checkpoint_minutes", "Checkpoint every N minutes", "int", ddc.CFG["checkpoint_minutes"], minimum=2, maximum=1440)],
                          submit="Save", intro="Saved to config.toml. A running dev box keeps the timers it started with; new ones use these. Worker idle applies at the next watchdog run.")

        def save(v: dict[str, Any] | None) -> None:
            if v:
                app.bg(lambda: [app.service.set_tunable(k, int(x)) for k, x in v.items()], lambda _: (app.notify("Saved to config.toml"), self._status()), "Save settings")
        app.push_screen(form, save)

    @on(Button.Pressed, "#b-stop")
    def action_stop_all(self) -> None:
        app: Any = self.app
        managed = [i for i in app.instances if i.managed]
        roles = getattr(app.provider, "stop_all_roles", None)   # None: the provider closes everything it created
        mine = [i for i in managed if roles is None or i.role in roles]
        kept = [i for i in managed if i not in mine]
        if not mine:
            if kept:
                app.notify("Nothing to stop here: " + ", ".join(i.name for i in kept) + " are not closed by Stop everything. Terminate them from Compute.", severity="warning", timeout=10)
            return
        notes = ["Other deployments on the account are never touched."]
        if kept:   # say so instead of implying they are closed: they keep billing
            notes.append("STAYS RUNNING and keeps billing (not closed by Stop everything): " + ", ".join(i.name for i in kept) + ". Terminate them from Compute.")
        confirm_destroy(app, "Stop everything?", mine, word="stop all", label="Stop all", notes=notes,
                        on_ok=lambda d: app.run_task("Stop everything", lambda log: app.provider.stop_all(log, **({"discard_workspace": True} if d else {}))))
