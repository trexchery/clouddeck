"""Monitor tab: spend, live utilization per instance, history, and the logs."""
from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import Label, RichLog, Select, Sparkline, Static

from .. import fmt
from .widgets import Card, Col, FilterTable, Pane, clean, kv_table
from .workers import util_text

LOG_SOURCES = [("CloudDeck events", "events"), ("ddc.log (controller)", "ddc"), ("Dev box · agent log", "agent"), ("Dev box · Claude window", "claude"),
               ("Dev box · workers window", "workers")]


class MonitorPane(Pane):
    primary = "#util"
    BINDINGS = [Binding("l", "pick_log", "Log source"), Binding("f", "follow", "Follow log")]

    def __init__(self) -> None:
        super().__init__()
        self.source = "events"
        self.follow = True
        self._last_text = ""
        self._remote_busy = False
        self._last_remote = 0.0

    def compose(self) -> ComposeResult:
        with Grid(id="cards"):
            yield Card("Balance", id="c-bal")
            yield Card("Burn rate", id="c-burn")
            yield Card("Spent today", id="c-today")
            yield Card("Running now", id="c-run")
            yield Card("Closed total", id="c-closed")
            yield Card("Worst case", id="c-worst")
        with Horizontal(classes="split", id="mon-main"):
            yield FilterTable([Col("Instance", "name"), Col("Role", "role"), Col("CPU", "cpu"), Col("RAM", "ram"), Col("Disk", "disk"), Col("GPU", "gpu"),
                               Col("Load", "load", right=True), Col("Job", "job"), Col("$/h", "usd", right=True), Col("Spent", "spent", right=True), Col("Left", "left", right=True)],
                              id="util", empty="Nothing running.")
            with Vertical(classes="detail", id="mon-side"):
                yield Label("CPU of selected instance (last probes)", classes="section")
                yield Sparkline([], id="cpuspark", summary_function=max)
                yield Label("Spend per day (14 d)", classes="section")
                yield Sparkline([], id="dayspark", summary_function=max)
                yield Static(id="breakdown")
        with Horizontal(id="logbar"):
            yield Label("Log:", classes="section")
            yield Select(LOG_SOURCES, value="events", allow_blank=False, id="logsrc")
            yield Label("", id="logstate", classes="hint")
        yield RichLog(id="log", wrap=True, markup=False, highlight=False, max_lines=3000)

    # ---- data
    def refresh_view(self, kind: str) -> None:
        if not self.is_mounted:
            return
        if kind in ("all", "instances", "probes", "money", "manual"):
            self._cards()
            self._util()
            self._side()
        if kind == "events" and self.source == "events":
            self._show_events()
        if kind in ("all", "manual"):
            self._load_source()

    def _cards(self) -> None:
        app: Any = self.app
        s = app.spend
        if not s:
            return
        run = [i for i in app.instances if i.managed and i.state == "running"]
        low = s.balance is not None and s.balance < 15
        self.query_one("#c-bal", Card).set(fmt.money(s.balance), "low balance!" if low else "guard on" if s.budget_guard else "budget guard off", "red" if low else "green")
        self.query_one("#c-burn", Card).set(fmt.rate(s.burn_hr), f"≈ {fmt.money(s.projected_24h)}/day")
        self.query_one("#c-today", Card).set(fmt.money(s.today), "closed + running")
        self.query_one("#c-run", Card).set(fmt.money(s.accrued_running, 3), f"{len(run)} instance{'s' * (len(run) != 1)} accruing")
        self.query_one("#c-closed", Card).set(fmt.money(s.closed_total), "all closed leases")
        self.query_one("#c-worst", Card).set(fmt.money(s.worst_case), "if all run to their limit", "yellow")

    def _util(self) -> None:
        app: Any = self.app
        rows = []
        for i in app.instances:
            if not i.managed:
                rows.append((i.id, [Text(i.name[:30], style="dim"), Text("external", style="dim"), "", "", "", "", "", "", f"{i.usd_hr:.3f}", "—", "—"]))
                continue
            p = app.probes.get(i.id)
            ok = bool(p and p.ok)
            gpu = util_text(p.gpu_util, 6) if ok and p.gpu_util is not None else Text("—", style="dim")
            rows.append((i.id, [Text(i.name.replace("ddc-", ""), style="bold"), i.role, util_text(p.cpu_frac if ok else None), util_text(p.mem_frac(i.mem_gb) if ok else None),
                                util_text(p.disk_frac if ok else None), gpu, f"{p.load:.1f}" if ok and p.load is not None else "—",
                                Text((p.job[:34] if ok and p.job else ("idle" if ok else (p.error[:30] if p else "…"))), style="dim" if not (ok and p.job) else ""),
                                f"{i.usd_hr:.3f}", fmt.money(i.spent_usd, 3), fmt.hours(i.remaining_h)]))
        self.query_one("#util", FilterTable).set_rows(rows)

    def _side(self) -> None:
        app: Any = self.app
        key = self.query_one("#util", FilterTable).selected_key
        self.query_one("#cpuspark", Sparkline).data = list(app.history.get(key, [])) or [0.0]
        s = app.spend
        if not s:
            return
        self.query_one("#dayspark", Sparkline).data = [v for _, v in s.daily] or [0.0]
        rows: list[tuple[str, Any]] = [("peak day", fmt.money(max((v for _, v in s.daily), default=0))), ("14-day total", fmt.money(sum(v for _, v in s.daily)))]
        for k, v in sorted(s.by_role.items(), key=lambda kv: -kv[1])[:4]:
            rows.append((f"role {k}", fmt.money(v)))
        for k, v in sorted(s.by_provider.items(), key=lambda kv: -kv[1])[:4]:
            rows.append((k.replace("provider.", "")[:22], fmt.money(v)))
        self.query_one("#breakdown", Static).update(kv_table(rows))

    @on(FilterTable.Highlighted)
    def _hl(self, e: FilterTable.Highlighted) -> None:
        self._side()

    # ---- logs
    @on(Select.Changed, "#logsrc")
    def _src(self, e: Select.Changed) -> None:
        self.source = str(e.value)
        self._load_source()

    def action_pick_log(self) -> None:
        self.query_one("#logsrc", Select).focus()
        self.query_one("#logsrc", Select).action_show_overlay()

    def action_follow(self) -> None:
        self.follow = not self.follow
        self.app.notify("following the log" if self.follow else "log frozen (f resumes)", timeout=2)  # type: ignore[attr-defined]
        if self.follow:
            self._load_source()

    def _write(self, text: str) -> None:
        text = clean(text)
        if text == self._last_text:
            return
        self._last_text = text
        log = self.query_one("#log", RichLog)
        log.clear()
        for line in text.splitlines()[-600:]:
            log.write(Text(line))

    def _show_events(self) -> None:
        if self.follow:
            self._write("\n".join(self.app.events))  # type: ignore[attr-defined]

    def _load_source(self) -> None:
        app: Any = self.app
        st = self.query_one("#logstate", Label)
        if self.source == "events":
            st.update("")
            self._show_events()
        elif self.source == "ddc":
            st.update("")
            self._write(app.service.local_log(300))
        else:
            dev = app.dev
            if not dev:
                st.update("no dev box running")
                self._write("")
                return
            if self._remote_busy:
                return
            self._remote_busy = True
            src = self.source

            def done(t: str) -> None:
                self._remote_busy = False
                self._last_remote = time.time()
                st.update(f"updated {time.strftime('%H:%M:%S')}")
                if src == self.source and self.follow:
                    self._write(t)

            def fail(e: BaseException) -> None:
                self._remote_busy = False
                st.update(str(e)[:80])
            app.bg(lambda: app.service.remote_log(dev, src, 300), done, "Fetch log", fail=fail)

    def on_mount(self) -> None:
        super().on_mount()
        self.set_interval(8, self._tick)

    def _tick(self) -> None:
        if self.is_mounted and self.follow and self.source != "events" and self.display and self.app.query_one("#tabs").active == "monitor":  # type: ignore[attr-defined]
            self._load_source()
