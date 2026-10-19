"""Compute tab: instances, provider catalogue with pricing, GPU market, and the deploy / resize form."""
from __future__ import annotations

from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, TabbedContent, TabPane

from .. import fmt
from ..engine import ddc
from ..providers.base import ROLE_LABEL, Instance, Offer, Quote, Spec, Verdict
from .picker import HardwareScreen
from .safety import confirm_destroy, guard_rows, refuse
from .widgets import Col, ConfirmScreen, FilterTable, Pane, kv_table

STATE_STYLE = {"running": "green", "provisioning": "yellow", "closed": "dim", "unknown": "red"}


def state_text(inst: Instance) -> Text:
    return Text(("● " if inst.state == "running" else "◌ ") + inst.state, style=STATE_STYLE.get(inst.state, ""))


def spec_of(inst: Instance, app: Any) -> Spec:
    """Starting point for a resize: the instance's current size and cap."""
    cap = max(inst.usd_hr * 2, 0.05)
    pr = ddc.CFG["profiles"].get(inst.meta.get("profile") or "", {})
    spec = Spec(cpu=inst.cpu or 8, mem_gb=int(inst.mem_gb or 16), disk_gb=int(inst.disk_gb or 50), gpu=inst.gpu, gpu_model=inst.gpu_model, role=inst.role,
                max_usd_hr=float(pr.get("max_usd_hr") or round(cap, 2)), hours=int(inst.limit_h or 4))
    try:   # the placement the instance was ordered with (arch, vendor, region, VRAM, pins ...) travels with a resize
        return spec.with_(**ext_of(Spec.from_profile(pr))) if pr else spec
    except (KeyError, TypeError, ValueError):
        return spec


EXT_FIELDS = ("gpu_vendor", "gpu_ram", "gpu_interface", "gpu_alt", "cpu_arch", "cpu_vendor", "region", "providers", "audited_only")


def ext_of(s: Spec) -> dict[str, Any]:
    """The placement fields of a spec (what the plain form has no inputs for)."""
    return {k: getattr(s, k) for k in EXT_FIELDS}


# ---------------------------------------------------------------------------------------------- deploy form
class DeployScreen(ModalScreen["Spec | None"]):
    """Configure CPU / RAM / storage / GPU, see a live price estimate and the worst case, then confirm."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "submit", "Deploy"), Binding("f2", "hardware", "Hardware…")]

    def __init__(self, base: Spec | None = None, *, title: str = "Deploy instance", lock_role: bool = False, submit: str = "Review & deploy",
                 replacing: Instance | None = None) -> None:
        super().__init__()
        self.base = base or Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", max_usd_hr=0.0, hours=4)
        self.title_, self.lock_role, self.submit_label = title, lock_role, submit
        self.replacing = replacing          # a resize: the spend guard counts this instance as already closed
        self._quote_timer: Any = None
        self._cap_touched = self.base.max_usd_hr > 0
        self._quote_seq = 0
        self.quote: Quote | None = None
        self.verdict: Verdict | None = None
        self.ext = ext_of(self.base)        # placement chosen in the hardware picker (arch, vendor, region, VRAM, pins ...); the preset dropdown clears it
        self._gpu_opts: list[tuple[str, str]] = [("any / not needed", "")] + ([(self.base.gpu_model, self.base.gpu_model)] if self.base.gpu_model else [])

    # ---- layout
    def compose(self) -> ComposeResult:
        app: Any = self.app
        b = self.base
        presets = app.provider.presets()
        with Vertical(id="dialog", classes="wide tall"):
            yield Label(self.title_, id="dialog-title")
            with Horizontal(classes="deploy-cols"):
                with VerticalScroll(id="form"):
                    yield Label("Role", classes="field-label")
                    yield Select([(ROLE_LABEL[r], r) for r in ROLE_LABEL], value=b.role, allow_blank=False, id="role", disabled=self.lock_role)
                    yield Label("Preset", classes="field-label")
                    yield Select([("Custom", "")] + [(f"{k}: {s.cpu} vCPU / {s.mem_gb} GB / {s.disk_gb} GB  cap ${s.max_usd_hr:.2f}/h", k) for k, s in presets.items()],
                                 value=b.preset if b.preset in presets else "", allow_blank=False, id="preset")
                    with Grid(classes="trio"):
                        yield Label("vCPU", classes="field-label")
                        yield Label("RAM GB", classes="field-label")
                        yield Label("Disk GB", classes="field-label")
                        yield Input(str(b.cpu), type="integer", id="cpu")
                        yield Input(str(b.mem_gb), type="integer", id="mem")
                        yield Input(str(b.disk_gb), type="integer", id="disk")
                    yield Label("GPU", classes="field-label")
                    with Horizontal(classes="gpu-row"):
                        yield Input(str(b.gpu), type="integer", id="gpu")
                        yield Select(self._gpu_opts, value=b.gpu_model, allow_blank=False,
                                     id="gpu_model", disabled=not app.provider.has("gpu"))
                    yield Label(self._placement_text(), id="placement", classes="hint")
                    with Grid(classes="duo"):
                        yield Label("Runtime limit (hours)", classes="field-label")
                        yield Label("Price cap ($/h)", classes="field-label")
                        yield Input(str(b.hours), type="integer", id="hours")
                        yield Input(f"{b.max_usd_hr:.2f}" if b.max_usd_hr else "", id="cap", placeholder="from estimate")
                    yield Label("", id="hours-note", classes="hint")
                yield Static(Text("Estimating…"), id="quote")
            yield Label("", id="err", classes="error")
            with Horizontal(classes="buttons"):
                yield Button(self.submit_label, variant="success", id="ok")
                if app.provider.has("hardware"):
                    yield Button("Hardware…", id="hw")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cpu", Input).focus()
        self._sync_role()
        app: Any = self.app
        if app.provider.has("gpu"):
            app.bg(app.provider.gpu_models, self._gpu_loaded, "GPU market")
        self._schedule()

    # ---- gpu options
    def _gpu_loaded(self, models: list[Any]) -> None:
        cur = self._wanted_model()
        self._gpu_opts = [("any / not needed", "")] + [(f"{g.model} · {g.ram} · {g.available}/{g.total} free · ~${g.price_avg:.2f}/h", g.model) for g in models]
        self._pick_model(cur)

    def _wanted_model(self) -> str:
        v = self.query_one("#gpu_model", Select).value
        return "" if v is Select.NULL else str(v)

    def _pick_model(self, model: str) -> None:
        """Select a model; one the market list does not know (a model typed in the picker, or one that appeared today) gets an entry of its own."""
        opts = list(self._gpu_opts)
        if model and model not in {v for _, v in opts}:
            opts.append((model, model))
        sel = self.query_one("#gpu_model", Select)
        with sel.prevent(Select.Changed):
            sel.set_options(opts)
            sel.value = model

    def _placement_text(self, spec: Spec | None = None) -> str:
        t = (spec or self.base).placement_label()
        return f"Placement: {t}" if t else ""

    # ---- hardware picker
    def action_hardware(self) -> None:
        if self.app.provider.has("hardware"):  # type: ignore[attr-defined]
            self.app.push_screen(HardwareScreen(self._spec(), replacing=self.replacing), self._hw_chosen)

    @on(Button.Pressed, "#hw")
    def _hw_pressed(self) -> None:
        self.action_hardware()

    def _hw_chosen(self, spec: Spec | None) -> None:
        if spec is None:
            return
        self.ext = ext_of(spec)
        for wid, v in (("cpu", spec.cpu), ("mem", spec.mem_gb), ("disk", spec.disk_gb), ("gpu", spec.gpu), ("hours", spec.hours), ("cap", f"{spec.max_usd_hr:.2f}" if spec.max_usd_hr else "")):
            self.query_one(f"#{wid}", Input).value = str(v)
        self._pick_model(spec.gpu_model)
        pre = self.query_one("#preset", Select)
        with pre.prevent(Select.Changed):
            pre.value = ""
        self._cap_touched = bool(spec.max_usd_hr)
        self.query_one("#placement", Label).update(self._placement_text(spec))
        self._schedule()

    # ---- reading the form
    def _int(self, wid: str) -> int:
        try:
            return int(self.query_one(f"#{wid}", Input).value or 0)
        except ValueError:
            return 0

    def _spec(self) -> Spec:
        role = str(self.query_one("#role", Select).value)
        preset = self.query_one("#preset", Select).value
        try:
            cap = float(self.query_one("#cap", Input).value or 0)
        except ValueError:
            cap = 0.0
        gm = self.query_one("#gpu_model", Select).value
        gpu = self._int("gpu")
        ext = self.ext if gpu else {**self.ext, "gpu_vendor": "", "gpu_ram": "", "gpu_interface": "", "gpu_alt": ()}   # GPU constraints mean nothing without a GPU
        return Spec(cpu=self._int("cpu"), mem_gb=self._int("mem"), disk_gb=self._int("disk"), gpu=gpu, gpu_model="" if gm is Select.NULL else str(gm),
                    role=role, max_usd_hr=cap if cap else (self.quote.cap_usd_hr if self.quote else 0.0), hours=self._int("hours"),
                    preset="" if preset is Select.NULL else str(preset), **ext)

    def _problems(self, spec: Spec) -> list[str]:
        app: Any = self.app
        bad = app.provider.problems(spec)
        if app.provider.has("workspace") and not self.lock_role:
            if spec.role == "dev" and app.dev:
                bad.append("a dev box is already running (use Resize on it, or terminate it first)")
            if spec.role == "worker" and not app.dev:
                bad.append("workers are driven from the dev box: deploy a dev box first")
        return bad

    # ---- live updates
    def _sync_role(self) -> None:
        dev = str(self.query_one("#role", Select).value) == "dev"
        h = self.query_one("#hours", Input)
        note = self.query_one("#hours-note", Label)
        if dev:
            h.value = str(int(ddc.CFG.get("max_runtime_hours", 8)))
            h.disabled = True
            note.update("The dev box limit follows config.toml so the final checkpoint always happens before the platform closes it.")
        else:
            h.disabled = False
            note.update("The platform closes the lease at this limit even if this PC is off.")

    @on(Select.Changed, "#role")
    def _role_changed(self, e: Select.Changed) -> None:
        self._sync_role()
        self._schedule()

    @on(Select.Changed, "#preset")
    def _preset_changed(self, e: Select.Changed) -> None:
        if e.value in (Select.NULL, ""):
            return
        s = self.app.provider.presets().get(str(e.value))  # type: ignore[attr-defined]
        if s:
            self.ext = ext_of(s)                         # a preset is a complete order: the picker's placement does not carry over
            self.query_one("#placement", Label).update(self._placement_text(s))
            for wid, v in (("cpu", s.cpu), ("mem", s.mem_gb), ("disk", s.disk_gb), ("gpu", s.gpu), ("cap", f"{s.max_usd_hr:.2f}")):
                self.query_one(f"#{wid}", Input).value = str(v)
            self._cap_touched = True
            self._schedule()

    @on(Select.Changed, "#gpu_model")
    def _gpu_changed(self, e: Select.Changed) -> None:
        if e.value not in (Select.NULL, "") and self._int("gpu") == 0:
            self.query_one("#gpu", Input).value = "1"
        self._schedule()

    @on(Input.Changed)
    def _input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "cap":
            self._cap_touched = bool(e.value.strip())
        self._schedule()

    def _schedule(self) -> None:
        if self._quote_timer:
            self._quote_timer.stop()
        self._quote_timer = self.set_timer(0.45, self._start_quote)

    def _start_quote(self) -> None:
        spec = self._spec()
        self._quote_seq += 1
        seq = self._quote_seq
        bad = self._problems(spec.with_(max_usd_hr=spec.max_usd_hr or 1.0))
        bad = [b for b in bad if "price cap" not in b]
        if bad:
            self.query_one("#err", Label).update(" · ".join(bad))
            self._render_quote(None, spec)
            return
        self.query_one("#err", Label).update("")
        app: Any = self.app

        def work() -> tuple[Quote, Verdict]:
            q = app.provider.quote(spec)
            capped = spec if spec.max_usd_hr else spec.with_(max_usd_hr=q.cap_usd_hr)
            try:
                v = app.provider.guard(capped, 1, self.replacing)
            except Exception as e:   # an unreadable state or ledger: the answer is no
                v = Verdict(False, f"could not check the spend guard: {e}", capped.max_usd_hr * capped.hours)
            return q, v
        app.bg(work, lambda r: self._got_quote(seq, *r), "Price estimate", fail=lambda e: self._quote_failed(seq, e))

    def _quote_failed(self, seq: int, e: BaseException) -> None:
        if seq == self._quote_seq:
            self.query_one("#quote", Static).update(Text(f"No estimate: {e}", style="red"))

    def _got_quote(self, seq: int, q: Quote, v: Verdict) -> None:
        if seq != self._quote_seq or not self.is_attached:
            return
        self.quote, self.verdict = q, v
        cap = self.query_one("#cap", Input)
        if not self._cap_touched:
            with cap.prevent(Input.Changed):
                cap.value = f"{q.cap_usd_hr:.2f}"
        self._render_quote(q, self._spec())

    def _render_quote(self, q: Quote | None, spec: Spec) -> None:
        app: Any = self.app
        box = self.query_one("#quote", Static)
        if q is None:
            box.update(Text("Fix the highlighted problems to see an estimate.", style="dim"))
            return
        worst = spec.max_usd_hr * spec.hours
        bal = app.spend.balance if app.spend else None
        rows: list[tuple[str, Any]] = [("Spec", spec.label()), *([("Placement", spec.placement_label())] if spec.placement_label() else []), ("Role", ROLE_LABEL.get(spec.role, spec.role)),
                                       ("Expected", Text(f"{fmt.rate(q.usd_hr)}  ≈ {fmt.money(q.usd_hr * 24)}/day", style="bold green")),
                                       ("basis", Text(q.basis, style="dim")),
                                       ("Price cap", Text(f"{fmt.rate(spec.max_usd_hr)} (no bid above it can win)")),
                                       ("Worst case", Text(f"{fmt.money(worst)} over {spec.hours} h", style="bold yellow"))]
        if self.verdict:
            rows += guard_rows(self.verdict)
        if spec.extended():
            rows.append(("", Text("Advertised capacity is not a confirmed bid: the real price is the bid you get after ordering; your cap stops anything above it.", style="dim")))
        if q.reference:
            ref = "  ".join(f"{k.upper()} {fmt.rate(v)}" for k, v in q.reference.items())
            rows.append(("Same size elsewhere", ref))
            if "akash" in q.reference and q.reference.get("aws") and q.usd_hr:
                rows.append(("vs AWS", Text(f"~{q.reference['aws'] / max(q.usd_hr, 1e-9):.0f}× cheaper than on-demand at the expected price", style="green")))
        if bal is not None:
            rows.append(("Balance", f"{fmt.money(bal)} → {fmt.money(bal - worst)} after the worst case"))
        s = app.spend
        if s and s.budget_guard is not None:
            rows.append(("Budget guard", "ON: spend limits enforced" if s.budget_guard else Text("OFF (budget_guard = false in config.toml): only price caps and runtime limits apply", style="bold yellow")))
        for n in q.notes:
            rows.append(("note", Text(n, style="dim")))
        box.update(kv_table(rows))

    # ---- submit
    @on(Button.Pressed, "#ok")
    def action_submit(self) -> None:
        spec = self._spec()
        bad = self._problems(spec)
        if bad:
            self.query_one("#err", Label).update(" · ".join(bad))
            return
        if self.verdict is not None and not self.verdict.ok and self.quote is not None and spec.max_usd_hr:
            self.query_one("#err", Label).update("Refused by the spend guard: " + self.verdict.text)
            return
        self.dismiss(spec)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------------------------- pane
class ComputePane(Pane):
    primary = "#inst"
    BINDINGS = [
        Binding("d", "deploy", "Deploy"), Binding("h", "hardware", "Hardware"), Binding("z", "resize", "Resize"), Binding("x", "terminate", "Terminate"),
        Binding("i", "sub('c-inst')", "Instances", show=False), Binding("p", "sub('c-prov')", "Providers"), Binding("g", "sub('c-gpu')", "GPUs"),
        Binding("f", "gpu_only", "GPU only", show=False), Binding("s", "sort", "Sort", show=False), Binding("u", "reload_market", "Reload market", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.offers: list[Offer] = []
        self.gpus: list[Any] = []
        self.market_loaded = False
        self.gpu_only = False

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="c-inst", id="ctabs"):
            with TabPane("Instances (i)", id="c-inst"):
                with Horizontal(classes="split"):
                    yield FilterTable([Col("Name", "name"), Col("Role", "role"), Col("State", "state"), Col("vCPU", "cpu", right=True), Col("RAM", "mem", right=True),
                                       Col("GPU", "gpu"), Col("$/h", "usd", right=True), Col("Up", "up", right=True), Col("Left", "left", right=True),
                                       Col("Spent", "spent", right=True), Col("Provider", "loc")],
                                      id="inst", empty="No instances. Press d to deploy one.")
                    yield Static(id="inst-detail", classes="detail")
            with TabPane("Providers (p)", id="c-prov"):
                with Horizontal(classes="split"):
                    yield FilterTable([Col("Provider", "name"), Col("Region", "region"), Col("Up 7d", "up", right=True), Col("Free vCPU", "cpu", right=True),
                                       Col("Free RAM", "mem", right=True), Col("GPU", "gpu"), Col("$/vCPU·h*", "price", right=True), Col("Speed", "speed", right=True),
                                       Col("CPU", "model")], id="prov", empty="Loading the provider catalogue…")
                    yield Static(id="prov-detail", classes="detail")
            with TabPane("GPUs (g)", id="c-gpu"):
                yield FilterTable([Col("Model", "model"), Col("VRAM", "ram"), Col("Free", "free", right=True), Col("Total", "total", right=True),
                                   Col("Providers", "provs", right=True), Col("Min $/h", "min", right=True), Col("Avg $/h", "avg", right=True), Col("Max $/h", "max", right=True)],
                                  id="gpu", empty="Loading the GPU market… (Enter on a model starts a deploy with it)")
        yield Label("* observed from your own past leases on that provider; bids are chosen automatically (best price ÷ measured speed under your cap).",
                    id="prov-note", classes="hint")

    # ---- palette
    def palette(self) -> list[SystemCommand]:
        return [SystemCommand("Deploy instance…", "Configure CPU/RAM/storage/GPU and rent an instance", self.action_deploy),
                SystemCommand("Browse hardware…", "Pick any advertised CPU or GPU configuration, compare providers", self.action_hardware),
                SystemCommand("Terminate selected instance…", "Close an instance created by CloudDeck", self.action_terminate),
                SystemCommand("Resize selected instance…", "Replace the selected instance with a different size", self.action_resize),
                SystemCommand("Browse providers", "Providers with uptime, free capacity and observed price", lambda: self._goto("c-prov")),
                SystemCommand("Browse GPU market", "GPU availability and price per hour", lambda: self._goto("c-gpu"))]

    def _goto(self, sub: str) -> None:
        self.app.action_tab("compute")  # type: ignore[attr-defined]
        self.action_sub(sub)

    # ---- data
    def refresh_view(self, kind: str) -> None:
        if not self.is_mounted:
            return
        if kind in ("all", "instances", "probes", "manual"):
            self._fill_instances()
        if kind in ("all", "manual") and not self.market_loaded:
            self.load_market()
        if kind == "manual" and self.market_loaded:
            self.load_market()

    def load_market(self) -> None:
        app: Any = self.app
        self.market_loaded = True

        def fetch() -> tuple[list[Offer], list[Any]]:
            return app.provider.offers(), (app.provider.gpu_models() if app.provider.has("gpu") else [])
        app.bg(fetch, self._market_loaded, "Provider catalogue")

    def action_reload_market(self) -> None:
        self.load_market()

    def _market_loaded(self, res: tuple[list[Offer], list[Any]]) -> None:
        self.offers, self.gpus = res
        self._fill_providers()
        self._fill_gpus()

    def _fill_instances(self) -> None:
        app: Any = self.app
        rows = []
        for i in app.instances:
            style = "dim" if not i.managed else ""
            rows.append((i.id, [Text(i.name[:30], style=style), Text(i.role, style=style), state_text(i), i.cpu or "—",
                                fmt.gb(i.mem_gb) if i.mem_gb else "—", f"{i.gpu}×{i.gpu_model or 'GPU'}" if i.gpu else "—", f"{i.usd_hr:.3f}",
                                fmt.hours(i.uptime_h) if i.started_at else "—", fmt.hours(i.remaining_h) if i.remaining_h is not None else "—",
                                fmt.money(i.spent_usd, 3) if i.managed else "—", Text(i.location.replace("provider.", "")[:28], style="dim")]))
        self.query_one("#inst", FilterTable).set_rows(rows)
        self._inst_detail()

    def _fill_providers(self) -> None:
        rows = []
        dup = {n for n in (o.name for o in self.offers) if sum(1 for o in self.offers if o.name == n) > 1}
        for o in self.offers:
            label = o.host.replace("provider.", "") if o.name in dup and o.host else o.name   # same org runs several providers
            gpu = Text(f"{o.gpu_free}× " + ",".join(o.gpus)[:22], style="green") if o.gpus else Text("—", style="dim")
            rows.append((o.id, [label[:30], o.region[:24], Text(fmt.pct(o.uptime), style="green" if o.uptime >= 0.98 else "yellow" if o.uptime >= 0.9 else "red"),
                                f"{o.cpu_free:.0f}", fmt.gb(o.mem_free_gb), gpu, f"{o.usd_per_vcpu_hr:.4f}" if o.usd_per_vcpu_hr else "—",
                                f"{o.speed:.2f}" if o.speed else "—", o.cpu_model[:24]],
                         [o.name.lower(), o.region.lower(), o.uptime, o.cpu_free, o.mem_free_gb, o.gpu_free, o.usd_per_vcpu_hr, o.speed, o.cpu_model.lower()]))
        t = self.query_one("#prov", FilterTable)
        t.filter_fn = (lambda k, c: c[5].plain != "—") if self.gpu_only else None
        t.set_rows(rows)
        self._prov_detail()

    def _fill_gpus(self) -> None:
        rows = [(f"{g.model}|{g.ram}", [g.model, g.ram, Text(str(g.available), style="green" if g.available else "red"), g.total, g.providers, f"{g.price_min:.2f}",
                                       Text(f"{g.price_avg:.2f}", style="bold"), f"{g.price_max:.2f}"],
                 [g.model, g.ram, g.available, g.total, g.providers, g.price_min, g.price_avg, g.price_max]) for g in self.gpus]
        self.query_one("#gpu", FilterTable).set_rows(rows)

    # ---- details
    @on(FilterTable.Highlighted)
    def _highlight(self, e: FilterTable.Highlighted) -> None:
        if e.table.id == "inst":
            self._inst_detail()
        elif e.table.id == "prov":
            self._prov_detail()

    def _inst_detail(self) -> None:
        app: Any = self.app
        box = self.query_one("#inst-detail", Static)
        inst = app.instance(self.query_one("#inst", FilterTable).selected_key)
        if not inst:
            box.update(Text("Select an instance to see its details.", style="dim"))
            return
        p = app.probes.get(inst.id)
        rows: list[tuple[str, Any]] = [("Name", Text(inst.name, style="bold")), ("Role", ROLE_LABEL.get(inst.role, inst.role)), ("State", state_text(inst)),
                                       ("Id", inst.id), ("Provider", inst.location or "—"), ("SSH", f"{inst.host}:{inst.port}" if inst.host else "—"),
                                       ("Size", f"{inst.cpu} vCPU · {fmt.gb(inst.mem_gb)} RAM · {fmt.gb(inst.disk_gb)} disk" + (f" · {inst.gpu}×{inst.gpu_model or 'GPU'}" if inst.gpu else "")),
                                       ("Price", fmt.rate(inst.usd_hr) + ("  (confirmed: the winning bid)" if inst.usd_hr and inst.meta.get("bids") else "")),
                                       *([("Bids received", Text("  ".join(f"{(b.get('host') or b.get('provider') or '?')[:18]} {fmt.rate(b.get('usd_hr'))}" for b in inst.meta["bids"]), style="dim"))]
                                         if inst.meta.get("bids") else []),
                                       ("Up", fmt.hours(inst.uptime_h) if inst.started_at else "—"),
                                       ("Platform limit", f"{fmt.hours(inst.remaining_h)} left of {inst.limit_h:.0f} h" if inst.remaining_h is not None else "—"),
                                       ("Spent", fmt.money(inst.spent_usd, 3)),
                                       ("If left running", fmt.money(inst.spent_usd + inst.usd_hr * (inst.remaining_h or 0)) + " by the limit" if inst.remaining_h is not None else "—")]
        if p and p.ok:
            rows += [("CPU", f"{fmt.bar(p.cpu_frac, 14)} {fmt.pct(p.cpu_frac)}  ({p.cores_used:.1f}/{p.cpus:.0f})"),
                     ("RAM", f"{fmt.bar(p.mem_frac(inst.mem_gb), 14)} {fmt.pct(p.mem_frac(inst.mem_gb))}")]
        if not inst.managed:
            rows.append(("", Text("Not created by CloudDeck: shown for your spend overview, never changed or closed from here.", style="yellow")))
        else:
            rows.append(("", Text("d deploy · z resize · x terminate · Enter " + ("attach Claude" if inst.role == "dev" else "open shell"), style="dim")))
        box.update(kv_table(rows))

    def _prov_detail(self) -> None:
        box = self.query_one("#prov-detail", Static)
        key = self.query_one("#prov", FilterTable).selected_key
        o = next((o for o in self.offers if o.id == key), None)
        if not o:
            box.update(Text("Select a provider.", style="dim"))
            return
        box.update(kv_table([("Provider", Text(o.name, style="bold")), ("Address", o.id[:20] + "…"), ("Host", o.host), ("Region", o.region or "—"),
                             ("Uptime 7d", fmt.pct(o.uptime)), ("Audited", "yes" if o.audited else "no"), ("Free", f"{o.cpu_free:.0f} vCPU · {fmt.gb(o.mem_free_gb)} RAM · {fmt.gb(o.disk_free_gb)} disk"),
                             ("GPUs", f"{o.gpu_free} free: {', '.join(o.gpus)}" if o.gpus else "none"), ("CPU", o.cpu_model or "unknown"), ("Memory", o.mem_type or "unknown"),
                             ("Speed", f"{o.speed:.2f}× reference single-thread" if o.speed else "—"),
                             ("Observed price", f"${o.usd_per_vcpu_hr:.4f}/vCPU·h → 32 vCPU ≈ {fmt.rate(o.usd_per_vcpu_hr * 32)}" if o.usd_per_vcpu_hr else "no lease of yours yet"),
                             ("", Text("Akash picks the provider by auction; this table is for choosing what to expect.", style="dim"))]))

    # ---- actions
    def action_sub(self, sub: str) -> None:
        self.query_one("#ctabs", TabbedContent).active = sub
        self.call_after_refresh(lambda: self.query_one("#" + {"c-inst": "inst", "c-prov": "prov", "c-gpu": "gpu"}[sub], FilterTable).focus_table())
        if sub != "c-inst" and not self.market_loaded:
            self.load_market()

    def _current_table(self) -> FilterTable:
        sub = self.query_one("#ctabs", TabbedContent).active
        return self.query_one("#" + {"c-inst": "inst", "c-prov": "prov", "c-gpu": "gpu"}[sub], FilterTable)

    def action_sort(self) -> None:
        self._current_table().cycle_sort()

    def action_gpu_only(self) -> None:
        self.gpu_only = not self.gpu_only
        self._fill_providers()
        self.app.notify("providers with GPUs only" if self.gpu_only else "all providers", timeout=2)

    @on(TabbedContent.TabActivated)
    def _sub_changed(self, e: TabbedContent.TabActivated) -> None:
        if e.tabbed_content.id == "ctabs":
            e.stop()
            if e.pane.id != "c-inst" and not self.market_loaded:
                self.load_market()

    def selected(self) -> Instance | None:
        return self.app.instance(self.query_one("#inst", FilterTable).selected_key)  # type: ignore[attr-defined]

    @on(FilterTable.Selected)
    def _row_selected(self, e: FilterTable.Selected) -> None:
        app: Any = self.app
        if e.table.id == "inst":
            inst = app.instance(e.key)
            if inst and inst.reachable:
                app.attach() if inst.role == "dev" else app.shell(inst)
            elif inst:
                app.notify("That instance is not reachable (external or not running).", severity="warning")
        elif e.table.id == "gpu":
            model = e.key.split("|")[0]
            self.action_deploy(Spec(cpu=8, mem_gb=32, disk_gb=100, gpu=1, gpu_model=model, role="box", hours=4))
        elif e.table.id == "prov" and app.provider.has("hardware"):   # a provider row: pick hardware pinned to it
            o = next((o for o in self.offers if o.id == e.key), None)
            if o:
                self.action_hardware(Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", hours=4, providers=(o.id,), region=o.region_code))

    def action_hardware(self, base: Spec | None = None) -> None:
        """The hardware picker; what it returns opens the normal deploy dialog (its review and spend guard still come first)."""
        app: Any = self.app
        if not app.provider.has("hardware"):
            app.notify(f"{app.provider.label} has no hardware picker.", severity="warning")
            return
        if not isinstance(base, Spec):
            base = Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", hours=4)
        app.push_screen(HardwareScreen(base), lambda spec: self.action_deploy(spec) if spec is not None else None)

    def action_deploy(self, base: Spec | None = None) -> None:
        app: Any = self.app
        if not isinstance(base, Spec):   # bound to a key: Textual passes nothing
            presets = app.provider.presets()
            if app.provider.has("workspace") and not app.dev:   # the first thing you need is the dev box
                pre = presets.get("32") or next(iter(presets.values()), Spec(cpu=32, mem_gb=64, disk_gb=100, max_usd_hr=0.9))
                base = pre.with_(role="dev", hours=int(ddc.CFG.get("max_runtime_hours", 8)))
            else:
                base = Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", hours=4)
        app.push_screen(DeployScreen(base), self._deploy_chosen)

    def _deploy_chosen(self, spec: Spec | None) -> None:
        if spec is None:
            return
        app: Any = self.app
        app.check_guard(spec, 1, None, lambda v: self._deploy_confirm(spec, v))

    def _deploy_confirm(self, spec: Spec, v: Verdict) -> None:
        app: Any = self.app
        if not v.ok:
            return refuse(app, v)
        extra: list[tuple[str, Any]] = []
        if spec.placement_label():
            extra.append(("Placement", spec.placement_label()))
        if spec.providers:
            extra.append(("Provider pin", Text("only bids from the chosen provider are accepted; if it does not bid, the order is closed and no lease is made", style="yellow")))
        if spec.extended():
            extra.append(("Prices", Text("Advertised capacity and estimates are not bids: the confirmed price is the bid you get after ordering, and your cap stops anything above it.", style="dim")))
        body = kv_table([("Instance", spec.label()), *extra, ("Role", ROLE_LABEL[spec.role]), ("Price cap", f"{fmt.rate(spec.max_usd_hr)}"),
                         ("Runtime limit", f"{spec.hours} h"), ("Cost ceiling", Text(f"cap {fmt.rate(spec.max_usd_hr)} × {spec.hours} h = {fmt.money(spec.max_usd_hr * spec.hours)}", style="bold yellow")),
                         *guard_rows(v)])
        app.push_screen(ConfirmScreen("Deploy this instance?", body, "Deploy"),
                        lambda ok: app.run_task(f"Deploy {spec.role} · {spec.label()}", lambda log: app.provider.deploy(spec, log)) if ok else None)

    def victims_of(self, inst: Instance) -> list[Instance]:
        """Everything that closing `inst` closes with it: the dev box takes its workers along (they cannot work without it)."""
        app: Any = self.app
        return [inst] + (list(app.fleet) if inst.role == "dev" else [])

    def action_terminate(self) -> None:
        app: Any = self.app
        inst = self.selected()
        if not inst:
            return
        if not inst.managed:
            app.notify("That deployment was not created by CloudDeck; it can only be closed in Akash Console.", severity="warning", timeout=8)
            return
        victims = self.victims_of(inst)
        notes = [f"{len(victims) - 1} worker(s) are closed with it: they cannot work without the dev box."] if len(victims) > 1 else []
        confirm_destroy(app, f"Terminate {inst.role}?", victims, word="stop dev" if inst.role == "dev" else "terminate", label="Terminate", notes=notes,
                        on_ok=lambda d: app.run_task(f"Terminate {inst.name}", lambda log: app.provider.terminate(inst.id, log, **({"discard_workspace": True} if d else {}))))

    def action_resize(self) -> None:
        app: Any = self.app
        inst = self.selected()
        if not inst or not inst.managed:
            app.notify("Select an instance created by CloudDeck.", severity="warning")
            return
        if not app.provider.has("resize"):
            app.notify(f"{app.provider.label} cannot resize.", severity="warning")
            return
        if inst.role == "job":
            app.notify("A job lease belongs to its `ddc job` run and cannot be resized.", severity="warning")
            return
        app.push_screen(DeployScreen(spec_of(inst, app), title=f"Resize {inst.name}", lock_role=True, submit="Review resize", replacing=inst),
                        lambda spec: self._resize_chosen(inst, spec))

    def _resize_chosen(self, inst: Instance, spec: Spec | None) -> None:
        if spec is None:
            return
        app: Any = self.app
        app.check_guard(spec, 1, inst, lambda v: self._resize_confirm(inst, spec, v))

    def _resize_confirm(self, inst: Instance, spec: Spec, v: Verdict) -> None:
        app: Any = self.app
        if not v.ok:
            return refuse(app, v)
        notes = ["Akash cannot change a running lease's resources, so this REPLACES it: the old instance is closed, then a new one is created."]
        victims = self.victims_of(inst)
        notes.append(f"The dev box is checkpointed (verified on this PC) first and restored onto the new lease; its {len(victims) - 1} worker(s) are closed." if inst.role == "dev"
                     else "Anything running on it stops.")
        confirm_destroy(app, f"Resize {inst.role}?", victims, word="resize", label="Replace", replacement=spec, verdict=v, notes=notes,
                        on_ok=lambda d: app.run_task(f"Resize {inst.name}", lambda log: app.provider.resize(inst, spec, log, **({"discard_workspace": True} if d else {}))))
