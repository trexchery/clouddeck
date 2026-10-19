"""Workers tab: distributed fleet table, node inspector, scale up/down, workload dispatch."""
from __future__ import annotations

import threading
import zlib
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RichLog, Select, Sparkline, Static

from .. import fmt
from ..engine import ddc
from ..providers.base import Instance, Spec, Verdict
from ..service import Probe
from .compute import spec_of, state_text
from .safety import confirm_destroy, guard_rows, refuse
from .widgets import Col, ConfirmScreen, Field, FilterTable, FormScreen, Pane, kv_table

PALETTE = ["cyan", "magenta", "green", "yellow", "blue", "bright_red", "bright_cyan", "bright_magenta", "bright_green", "bright_yellow"]


def util_text(frac: float | None, width: int = 8) -> Text:
    if frac is None:
        return Text("·" * width + "  —", style="dim")
    style = "green" if frac < 0.6 else "yellow" if frac < 0.85 else "bold red"
    return Text(f"{fmt.bar(frac, width)} {frac * 100:3.0f}%", style=style)


def host_style(host: str) -> str:
    return PALETTE[zlib.crc32(host.encode()) % len(PALETTE)]


class NodeScreen(ModalScreen[None]):
    """Live inspector for one node: refreshes its probe every few seconds."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("a", "shell", "Shell"), Binding("x", "terminate", "Terminate")]

    def __init__(self, inst: Instance) -> None:
        super().__init__()
        self.inst = inst
        self._timer: Any = None
        self.probe: Probe | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label(f"Node {self.inst.name}", id="dialog-title")
            yield Static(id="node-info")
            yield Label("CPU history", classes="field-label")
            yield Sparkline([], id="node-spark", summary_function=max)
            with Horizontal(classes="buttons"):
                yield Button("Shell (a)", id="shell")
                yield Button("Terminate (x)", id="term", variant="error")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.set_interval(6, self._reload)
        self._reload()
        self._paint()

    def _reload(self) -> None:
        app: Any = self.app
        cur = app.instance(self.inst.id)
        if cur:
            self.inst = cur
        app.bg(lambda: app.service.probe(self.inst), self._got, "Probe")

    def _got(self, p: Probe) -> None:
        self.probe = p
        app: Any = self.app
        if p.ok and p.cpu_frac is not None:
            app._got_probes({self.inst.id: p})
        self._paint()

    def _paint(self) -> None:   # not named _render: that is Textual's own method
        if not self.is_mounted or not self.is_attached:   # a probe can finish after the inspector was closed
            return
        app: Any = self.app
        i, p = self.inst, self.probe or app.probes.get(self.inst.id)
        rows: list[tuple[str, Any]] = [("State", state_text(i)), ("Provider", i.location or "—"), ("SSH", f"{i.host}:{i.port}"),
                                       ("Size", f"{i.cpu} vCPU · {fmt.gb(i.mem_gb)} RAM · {fmt.gb(i.disk_gb)} disk"), ("Price", f"{fmt.rate(i.usd_hr)}   spent {fmt.money(i.spent_usd, 3)}"),
                                       ("Up", f"{fmt.hours(i.uptime_h)}   platform limit in {fmt.hours(i.remaining_h)}")]
        if p and p.ok:
            rows += [("CPU", util_text(p.cpu_frac, 20).append(f"   {p.cores_used:.1f} of {p.cpus:.0f} cores")),
                     ("RAM", util_text(p.mem_frac(i.mem_gb), 20).append(f"   {fmt.gb(p.mem_used_gb)} of {fmt.gb(p.mem_max_gb or i.mem_gb)}")),
                     ("Disk", util_text(p.disk_frac, 20).append(f"   {fmt.gb(p.disk_used_gb)} of {fmt.gb(p.disk_gb)}")), ("Load", f"{p.load}"),
                     ("Job", p.job or Text("idle", style="dim")), ("Top", p.top or "—")]
            if p.gpu_util is not None:
                rows.append(("GPU", util_text(p.gpu_util, 20).append(f"   VRAM {p.gpu_mem_used_mb:.0f}/{p.gpu_mem_mb:.0f} MB")))
        elif p:
            rows.append(("Probe", Text(p.error or "no data", style="red")))
        else:
            rows.append(("Probe", Text("measuring…", style="dim")))
        self.query_one("#node-info", Static).update(kv_table(rows))
        self.query_one("#node-spark", Sparkline).data = list(app.history.get(i.id, [])) or [0.0]

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#shell")
    def action_shell(self) -> None:
        self.dismiss(None)
        self.app.shell(self.inst)  # type: ignore[attr-defined]

    @on(Button.Pressed, "#term")
    def action_terminate(self) -> None:
        self.dismiss(None)
        pane = self.app.active_pane()  # type: ignore[attr-defined]
        if hasattr(pane, "terminate_instance"):
            pane.terminate_instance(self.inst)


class DispatchScreen(ModalScreen[None]):
    """Run a command on every worker (or the selected node / dev box) and watch the merged output."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, targets: dict[str, list[Instance]], default_target: str) -> None:
        super().__init__()
        self.targets = targets
        self.default_target = default_target
        self.stop_event = threading.Event()
        self.running = False

    def compose(self) -> ComposeResult:
        app: Any = self.app
        loads = app.service.workloads()
        with Vertical(id="dialog", classes="wide tall"):
            yield Label("Dispatch workload", id="dialog-title")
            with Vertical(id="dispatch-form"):
                yield Label("Workload", classes="field-label")
                yield Select([(f"{w['name']} — {w.get('description', w['command'][:50])}", i) for i, w in enumerate(loads)] + [("Custom command…", -1)],
                             value=0, allow_blank=False, id="wl")
                yield Label("Command (runs as user 'dev' through bash)", classes="field-label")
                yield Input(loads[0]["command"], id="cmd")
                yield Label("Target", classes="field-label")
                yield Select([(f"{k} ({len(v)})", k) for k, v in self.targets.items()], value=self.default_target, allow_blank=False, id="target")
                yield Label("Working directory (optional)", classes="field-label")
                yield Input("", id="cwd", placeholder="e.g. /work/my-repo")
            yield RichLog(id="out", wrap=True, markup=False, highlight=False, max_lines=5000)
            yield Label("", id="dstatus", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Run", variant="success", id="run")
                yield Button("Stop", variant="warning", id="stop", disabled=True)
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.query_one("#out").display = False
        self.query_one("#cmd", Input).focus()

    @on(Select.Changed, "#wl")
    def _wl(self, e: Select.Changed) -> None:
        loads = self.app.service.workloads()  # type: ignore[attr-defined]
        if isinstance(e.value, int) and 0 <= e.value < len(loads):
            self.query_one("#cmd", Input).value = loads[e.value]["command"]
            want = "Dev box" if loads[e.value].get("target") == "dev" else "All workers"
            if want in self.targets:
                self.query_one("#target", Select).value = want

    @on(Button.Pressed, "#run")
    def _run(self) -> None:
        cmd = self.query_one("#cmd", Input).value.strip()
        tgt = self.query_one("#target", Select).value
        insts = self.targets.get(str(tgt), [])
        if not cmd or not insts:
            self.query_one("#dstatus", Label).update("Need a command and at least one running target.")
            return
        cwd = self.query_one("#cwd", Input).value.strip()
        self.query_one("#dispatch-form").display = False
        out = self.query_one("#out", RichLog)
        out.display = True
        self.query_one("#run", Button).disabled = True
        self.query_one("#stop", Button).disabled = False
        self.running = True
        self.stop_event.clear()
        app: Any = self.app
        app.log_event(f"dispatch `{cmd[:60]}` to {len(insts)} node(s)")

        def on_line(host: str, line: str) -> None:
            t = Text()
            t.append(f"{host:>5} │ ", style=host_style(host))
            t.append(line)
            app.ui(out.write, t)

        def job() -> None:
            try:
                res = app.service.dispatch(cmd, insts, on_line, self.stop_event, cwd)
                ok = sum(1 for r in res if r.code == 0)
                msg = Text(f"finished: {ok} ok, {len(res) - ok} failed", style="green" if ok == len(res) else "bold red")
            except Exception as e:
                msg = Text(f"failed: {e}", style="bold red")
            app.ui(self._finished, msg)
        app.busy += 1
        self.run_worker(job, thread=True, exit_on_error=False)

    def _finished(self, msg: Text) -> None:
        self.app.busy = max(0, self.app.busy - 1)  # type: ignore[attr-defined]
        self.running = False
        if self.is_mounted:
            self.query_one("#dstatus", Label).update(msg)
            self.query_one("#stop", Button).disabled = True
            self.query_one("#out", RichLog).write(msg)

    @on(Button.Pressed, "#stop")
    def _stop(self) -> None:
        self.stop_event.set()
        self.query_one("#dstatus", Label).update("stopping…")

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        if self.running:
            self.stop_event.set()
        self.dismiss(None)


class WorkersPane(Pane):
    primary = "#fleet"
    BINDINGS = [
        Binding("plus,equals_sign", "add_one", "+1 worker"), Binding("s", "scale", "Scale"), Binding("d", "dispatch", "Dispatch"),
        Binding("x", "terminate", "Terminate"), Binding("h", "heal", "Heal"), Binding("a", "shell", "Shell", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(id="fleet-summary")
        with Horizontal(classes="split"):
            yield FilterTable([Col("Node", "node"), Col("State", "state"), Col("vCPU", "cpu", right=True), Col("CPU", "cpuu"), Col("RAM", "ramu"), Col("Job", "job"),
                               Col("$/h", "usd", right=True), Col("Up", "up", right=True), Col("Left", "left", right=True), Col("Provider", "loc")],
                              id="fleet", empty="No workers. Press s to scale up (needs a running dev box).")
            with Vertical(classes="detail", id="fleet-side"):
                yield Static(id="fleet-detail")
                yield Label("CPU history", classes="field-label")
                yield Sparkline([], id="fleet-spark", summary_function=max)

    def palette(self) -> list[SystemCommand]:
        return [SystemCommand("Scale workers to N…", "Grow or shrink the worker fleet", self.action_scale),
                SystemCommand("Add one worker", "Deploy one more worker like the others", self.action_add_one),
                SystemCommand("Dispatch workload…", "Run a command on every worker", self.action_dispatch),
                SystemCommand("Heal workers", "Re-provision workers whose container was restarted", self.action_heal)]

    # ---- view
    def refresh_view(self, kind: str) -> None:
        if not self.is_mounted or kind in ("events", "money", "idle"):
            return
        app: Any = self.app
        ws = app.fleet
        rows = []
        for w in ws:
            p = app.probes.get(w.id)
            job = (p.job if p and p.ok else "") or ""
            rows.append((w.id, [Text(w.name.replace("ddc-", ""), style="bold"), state_text(w), w.cpu, util_text(p.cpu_frac if p and p.ok else None),
                                util_text(p.mem_frac(w.mem_gb) if p and p.ok else None), Text(job[:38] or ("idle" if p and p.ok else (p.error[:30] if p else "…")), style="" if job else "dim"),
                                f"{w.usd_hr:.3f}", fmt.hours(w.uptime_h), fmt.hours(w.remaining_h), Text(w.location.replace("provider.", "")[:26], style="dim")],
                         [w.name, w.state, w.cpu, p.cpu_frac if p and p.ok else None, p.mem_frac(w.mem_gb) if p and p.ok else None, job, w.usd_hr, w.uptime_h,
                          w.remaining_h or 0, w.location]))
        self.query_one("#fleet", FilterTable).set_rows(rows)
        live = [app.probes[w.id] for w in ws if w.id in app.probes and app.probes[w.id].ok]
        vcpu = sum(w.cpu for w in ws)
        busy = (sum(p.cores_used for p in live) / max(1.0, sum(p.cpus for p in live))) if live else None
        burn = sum(w.usd_hr for w in ws)
        left = sum(w.usd_hr * (w.remaining_h or 0) for w in ws)
        t = Text()
        t.append(f" {len(ws)} workers", style="bold")
        t.append(f"  ·  {vcpu} vCPU  ·  ", style="dim")
        t.append(fmt.rate(burn), style="bold")
        t.append("  ·  busy ", style="dim")
        t.append(fmt.pct(busy), style="bold")
        t.append(f"  ·  worst case to their limits {fmt.money(left)}", style="dim")
        self.query_one("#fleet-summary", Static).update(t)
        self._detail()

    @on(FilterTable.Highlighted)
    def _hl(self, e: FilterTable.Highlighted) -> None:
        self._detail()

    def _detail(self) -> None:
        app: Any = self.app
        w = app.instance(self.query_one("#fleet", FilterTable).selected_key)
        box = self.query_one("#fleet-detail", Static)
        if not w:
            box.update(Text("Select a node. Enter opens the live inspector.", style="dim"))
            self.query_one("#fleet-spark", Sparkline).data = [0.0]
            return
        p = app.probes.get(w.id)
        rows: list[tuple[str, Any]] = [("Node", Text(w.name, style="bold")), ("Provider", w.location or "—"), ("SSH", f"{w.host}:{w.port}"),
                                       ("Size", f"{w.cpu} vCPU · {fmt.gb(w.mem_gb)} · {fmt.gb(w.disk_gb)}"), ("Spent", fmt.money(w.spent_usd, 3))]
        if p and p.ok:
            rows += [("CPU", util_text(p.cpu_frac, 12)), ("RAM", util_text(p.mem_frac(w.mem_gb), 12)), ("Disk", util_text(p.disk_frac, 12)), ("Top", p.top or "—")]
        elif p:
            rows.append(("Probe", Text(p.error or "no data", style="red")))
        box.update(kv_table(rows))
        self.query_one("#fleet-spark", Sparkline).data = list(app.history.get(w.id, [])) or [0.0]

    def selected(self) -> Instance | None:
        return self.app.instance(self.query_one("#fleet", FilterTable).selected_key)  # type: ignore[attr-defined]

    @on(FilterTable.Selected)
    def _open(self, e: FilterTable.Selected) -> None:
        inst = self.app.instance(e.key)  # type: ignore[attr-defined]
        if inst:
            self.app.push_screen(NodeScreen(inst))

    # ---- actions
    def _need_fleet(self) -> bool:
        app: Any = self.app
        if not app.provider.has("fleet"):
            app.notify(f"{app.provider.label} has no worker fleets.", severity="warning")
            return False
        return True

    def _worker_spec(self) -> Spec | None:
        app: Any = self.app
        if app.fleet:
            return spec_of(app.fleet[0], app).with_(role="worker", hours=int(ddc.CFG.get("worker_runtime_hours", 4)))
        pre = app.provider.presets().get("32") or next(iter(app.provider.presets().values()), None)
        return pre.with_(role="worker", hours=int(ddc.CFG.get("worker_runtime_hours", 4))) if pre else None

    def action_add_one(self) -> None:
        app: Any = self.app
        if not self._need_fleet():
            return
        spec = self._worker_spec()
        if not spec:
            app.notify("No size to copy: use s (scale) and pick a preset.", severity="warning")
            return
        if not app.dev and app.provider.has("workspace"):
            app.notify("Start the dev box first (Compute → d); workers are driven from it.", severity="warning")
            return
        app.check_guard(spec, 1, None, lambda v: self._add_confirm(spec, v))

    def _add_confirm(self, spec: Spec, v: Verdict) -> None:
        app: Any = self.app
        if not v.ok:
            return refuse(app, v)
        body = kv_table([("Add", f"1 × {spec.label()}"), ("Price cap", fmt.rate(spec.max_usd_hr)), ("Runtime limit", f"{spec.hours} h"), *guard_rows(v)])
        app.push_screen(ConfirmScreen("Add one worker?", body, "Deploy"),
                        lambda ok: app.run_task("Deploy worker", lambda log: app.provider.deploy(spec, log)) if ok else None)

    def action_scale(self) -> None:
        app: Any = self.app
        if not self._need_fleet():
            return
        presets = app.provider.presets()
        if not presets:
            app.notify("This provider has no size presets; add workers one by one with +.", severity="warning")
            return
        cur = len(app.fleet)
        have = (app.fleet[0].meta.get("profile") if app.fleet else "") or ""
        form = FormScreen("Scale workers", [Field("n", "Target number of workers", "int", cur, minimum=0, maximum=64, hint=f"Currently {cur}. 0 removes all workers."),
                                            Field("preset", "Size of new workers", "select", have if have in presets else next(iter(presets)),
                                                  options=[(f"{k}: {s.cpu} vCPU / {s.mem_gb} GB — cap ${s.max_usd_hr:.2f}/h", k) for k, s in presets.items()])],
                          submit="Review", intro="Workers hold no secrets. Each new worker's runtime limit follows config.toml (worker_runtime_hours).")
        app.push_screen(form, self._scale_chosen)

    def _scale_chosen(self, v: dict[str, Any] | None) -> None:
        if not v:
            return
        app: Any = self.app
        n, cur = int(v["n"]), len(app.fleet)
        if n == cur:
            app.notify("Already at that size.", timeout=3)
            return
        spec = app.provider.presets()[v["preset"]].with_(role="worker", hours=int(ddc.CFG.get("worker_runtime_hours", 4)))
        if n > cur:
            app.check_guard(spec, n - cur, None, lambda v: self._scale_up_confirm(cur, n, spec, v))
        else:
            try:
                victims = app.provider.scale_victims(n)
            except Exception as e:   # e.g. a damaged state file: do not guess which workers would be closed
                app.notify(str(e)[:400], title="Cannot list the workers to close", severity="error", timeout=12)
                return
            confirm_destroy(app, f"Scale {cur} → {n}?", victims, word="scale down", label="Scale down", on_ok=lambda d: self._do_scale(n, spec),
                            notes=["Anything running on them stops."])

    def _scale_up_confirm(self, cur: int, n: int, spec: Spec, v: Verdict) -> None:
        app: Any = self.app
        if not v.ok:
            return refuse(app, v)
        body = kv_table([("Add", f"{n - cur} × {spec.label()}"), ("Price cap", f"{fmt.rate(spec.max_usd_hr)} each"), ("Runtime limit", f"{spec.hours} h"), *guard_rows(v)])
        app.push_screen(ConfirmScreen(f"Scale {cur} → {n}?", body, "Scale up"), lambda ok: self._do_scale(n, spec) if ok else None)

    def _do_scale(self, n: int, spec: Spec) -> None:
        app: Any = self.app
        app.run_task(f"Scale workers to {n}", lambda log: app.provider.scale(n, spec, log))

    def terminate_instance(self, w: Instance) -> None:
        app: Any = self.app
        confirm_destroy(app, f"Terminate {w.role}?", [w], word="terminate", label="Terminate", notes=["Anything running on it stops."],
                        on_ok=lambda d: app.run_task(f"Terminate {w.name}", lambda log: app.provider.terminate(w.id, log)))

    def action_terminate(self) -> None:
        if w := self.selected():
            self.terminate_instance(w)

    def action_heal(self) -> None:
        app: Any = self.app
        if app.provider.has("heal"):
            app.run_task("Heal workers", lambda log: app.provider.heal(log))
        else:
            app.notify("Not supported by this provider.", severity="warning")

    def action_shell(self) -> None:
        if w := self.selected():
            self.app.shell(w)  # type: ignore[attr-defined]

    def action_dispatch(self) -> None:
        app: Any = self.app
        sel = self.selected()
        targets: dict[str, list[Instance]] = {"All workers": [w for w in app.fleet if w.reachable]}
        if sel and sel.reachable:
            targets[f"Selected: {sel.name.replace('ddc-', '')}"] = [sel]
        if app.dev:
            targets["Dev box"] = [app.dev]
        if not any(targets.values()):
            app.notify("No running nodes to dispatch to.", severity="warning")
            return
        app.push_screen(DispatchScreen(targets, "All workers" if targets["All workers"] else "Dev box"))
