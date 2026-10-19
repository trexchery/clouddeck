"""Hardware picker: choose ANY currently advertised CPU or GPU configuration instead of a preset.

Everything in the lists comes from `provider.catalog()` (vendors, models, VRAM / interface variants, architectures, regions): nothing is
hardcoded, so hardware that appears on the network later shows up without a code change.  What is shown is labelled by how sure it is:

  advertised  what providers report (an upper bound) with a price ESTIMATE whose source is named
  screened    the network says a node of that provider fits this exact order (after you press "Check availability"; still unpriced)
  confirmed   a real bid: only exists after an order is placed through the normal deploy flow, never here

The picker never deploys.  It returns a `Spec`; the deploy dialog and its spend-guard review take it from there."""
from __future__ import annotations

import re
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static, Switch

from .. import fmt, hardware
from ..hardware import Match, Screening
from ..providers.base import Quote, Spec, Verdict, norm_model
from .widgets import Col, FilterTable, kv_table

OTHER = "__other__"
MAX_MARKS = 3
BADGE = {hardware.ADVERTISED: ("◌ advertised", "yellow"), hardware.SCREENED: ("● screened", "green"), hardware.CONFIRMED: ("✔ confirmed", "bold green")}
SOURCE = {hardware.PRICE_PROVIDER: "own history", hardware.PRICE_MARKET: "market avg", hardware.PRICE_NONE: "—"}
DISCLAIMER = ("Advertised capacity is what providers report (an upper bound, not a promise).  Screened means the network found a node that fits; it is still unpriced.  "
              "Prices here are estimates; the confirmed price is the bid you get after ordering, and your price cap stops anything above it.")


def order_key(s: Spec) -> tuple[Any, ...]:
    """What an availability check is valid for (price cap and runtime do not change who can host it)."""
    return (s.cpu, s.mem_gb, s.disk_gb, s.gpu, s.gpu_model, s.gpu_alt, s.gpu_vendor, s.gpu_ram, s.gpu_interface, s.cpu_arch, s.cpu_vendor, s.region, s.audited_only, s.providers)


def short(owner: str) -> str:
    return owner[:10] + "…" + owner[-4:] if len(owner) > 16 else owner


def match_row(m: Match, marked: bool) -> tuple[str, list[Any], list[Any]]:
    o = m.offer
    badge, style = BADGE.get(m.evidence, (m.evidence, ""))
    fit = f"{o.cpu_free:.0f} vCPU" + (f" · {m.free_gpus} GPU" if m.free_gpus is not None else "")
    est = f"~${m.est_usd_hr:.2f}" if m.est_usd_hr is not None else "—"
    inc = "—" if m.incidents_7d is None else str(m.incidents_7d)
    region = o.region_code or (f"~{o.region}" if o.region else "—")
    cells = [Text("●" if marked else " "), o.name[:26], region[:20], "yes" if o.audited else "no", fmt.pct(o.uptime) if o.uptime else "—", inc, fit,
             Text(badge, style=style), est, SOURCE.get(m.price_source, "—")]
    sort = [marked, o.name.lower(), region, o.audited, o.uptime, m.incidents_7d, o.cpu_free, m.evidence, m.est_usd_hr, m.price_source]
    return o.id, cells, sort


def compare_table(ms: list[Match]) -> Table:
    """One column per offer, side by side."""
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", no_wrap=True)
    for _ in ms:
        t.add_column(overflow="fold")

    def row(label: str, f: Any) -> None:
        t.add_row(label, *[f(m) if not isinstance(f(m), str) else Text(f(m)) for m in ms])
    row("Provider", lambda m: Text(m.offer.name, style="bold"))
    row("Address", lambda m: short(m.offer.id))
    row("Region", lambda m: (m.offer.region_code or "not stated") + (f" ({m.offer.region})" if m.offer.region else ""))
    row("Audited", lambda m: "yes" if m.offer.audited else "no")
    row("Uptime 7d", lambda m: fmt.pct(m.offer.uptime) if m.offer.uptime else "—")
    row("Incidents 7d", lambda m: "—" if m.incidents_7d is None else str(m.incidents_7d))
    row("Free vCPU", lambda m: f"{m.offer.cpu_free:.0f}")
    row("Free RAM / disk", lambda m: f"{fmt.gb(m.offer.mem_free_gb)} / {fmt.gb(m.offer.disk_free_gb)}")
    row("GPUs it has", lambda m: ", ".join(v.label() for v in m.offer.gpu_variants) or "none stated")
    row("Free GPUs", lambda m: "—" if m.free_gpus is None else str(m.free_gpus))
    row("CPU", lambda m: " ".join(x for x in (m.offer.cpu_vendor, m.offer.cpu_arch) if x) or "not stated")
    row("CPU model", lambda m: m.offer.cpu_model or "unknown")
    row("Memory", lambda m: m.offer.mem_type or "unknown")
    row("Speed", lambda m: f"{m.offer.speed:.2f}× reference" if m.offer.speed else "—")
    row("State", lambda m: Text(BADGE.get(m.evidence, (m.evidence, ""))[0], style=BADGE.get(m.evidence, ("", ""))[1]))
    row("Estimate", lambda m: f"~${m.est_usd_hr:.3f}/h ({SOURCE.get(m.price_source, '—')})" if m.est_usd_hr is not None else "unknown")
    row("Your price here", lambda m: f"${m.offer.usd_per_vcpu_hr:.4f}/vCPU·h" if m.offer.usd_per_vcpu_hr else "no lease of yours yet")
    row("Notes", lambda m: "; ".join(m.notes) or "—")
    return t


class CompareScreen(ModalScreen["str | None"]):
    """2-3 offers side by side.  Choosing one returns its provider address (the picker pins it)."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, ms: list[Match]) -> None:
        super().__init__()
        self.ms = ms

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label("Compare offers", id="dialog-title")
            with VerticalScroll():
                yield Static(compare_table(self.ms), id="cmp")
            yield Label("No price here is a bid: confirmed prices only exist after an order is placed.", classes="hint")
            with Horizontal(classes="buttons"):
                for i, m in enumerate(self.ms):
                    yield Button(f"Pin {m.offer.name[:18]}", id=f"pick-{i}")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.query_one("#pick-0", Button).focus()

    @on(Button.Pressed)
    def _pressed(self, e: Button.Pressed) -> None:
        bid = e.button.id or ""
        if bid.startswith("pick-"):
            self.dismiss(self.ms[int(bid[5:])].offer.id)
        else:
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class HardwareScreen(ModalScreen["Spec | None"]):
    """CPU / GPU picker with live offers.  Returns a Spec (never deploys)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "accept", "Use hardware"), Binding("space", "mark", "Mark offer", show=False),
                Binding("c", "compare", "Compare", show=False), Binding("a", "check", "Check availability", show=False), Binding("u", "unpin", "Unpin", show=False),
                Binding("s", "sort", "Sort", show=False)]

    def __init__(self, base: Spec | None = None, *, title: str = "Choose hardware", replacing: Any = None) -> None:
        super().__init__()
        self.base = base or Spec(cpu=8, mem_gb=16, disk_gb=50, role="box", max_usd_hr=0.0, hours=4)
        self.title_, self.replacing = title, replacing
        self.cat: hardware.HardwareCatalog | None = None
        self.adv: list[Match] = []
        self.screening: Screening | None = None
        self.screen_key: tuple[Any, ...] | None = None
        self.pin: tuple[str, str] | None = (self.base.providers[0], self.base.providers[0]) if self.base.providers else None
        self.marked: list[str] = []
        self.quote: Quote | None = None
        self.verdict: Verdict | None = None
        self.explain = ""          # why the table is empty / what is wrong with the data
        self._seq = 0
        self._debounce: Any = None
        self._cap_touched = self.base.max_usd_hr > 0
        self._checking = False
        self._awaiting_catalog = True
        self._rows: dict[str, Match] = {}
        self._regions: set[str] = set()
        self.dirty = False         # True from a change in the form until the offers for it have been drawn (what a test waits on)

    # ------------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        b = self.base
        loading = [("loading…", "")]
        with Vertical(id="dialog", classes="wide tall hw"):
            yield Label(self.title_, id="dialog-title")
            with RadioSet(id="mode"):
                yield RadioButton("CPU only", id="m-cpu", value=not b.gpu)
                yield RadioButton("GPU", id="m-gpu", value=bool(b.gpu))
            with Horizontal(classes="deploy-cols hw-cols"):
                with VerticalScroll(id="form"):
                    yield Label("", id="hw-status", classes="hint")
                    with Vertical(id="gpu-box"):
                        yield Label("GPU vendor", classes="field-label")
                        yield Select(loading, allow_blank=False, id="g_vendor")
                        yield Input(b.gpu_vendor, placeholder="vendor id, e.g. nvidia", id="g_vendor_text")
                        yield Label("GPU model", classes="field-label")
                        yield Select(loading, allow_blank=False, id="g_model")
                        yield Input(b.gpu_model, placeholder="model id exactly as providers advertise it", id="g_model_text")
                        yield Label("Also accept these models (any of, comma separated)", classes="field-label")
                        yield Input(", ".join(b.gpu_alt), placeholder="optional, e.g. a100, h100", id="g_alt")
                        yield Label("VRAM / interface variant", classes="field-label")
                        yield Select(loading, allow_blank=False, id="g_variant")
                        yield Label("Number of GPUs", classes="field-label")
                        yield Input(str(b.gpu or 1), type="integer", id="gpu")
                        yield Label("", id="gpu-hint", classes="hint")
                    yield Label("CPU architecture", classes="field-label")
                    yield Select(loading, allow_blank=False, id="c_arch")
                    yield Label("CPU vendor", classes="field-label", id="c_vendor-label")
                    yield Select(loading, allow_blank=False, id="c_vendor")
                    yield Label("", id="cpu-note", classes="hint")
                    with Horizontal(classes="trio-row"):
                        yield Label("vCPU", classes="field-label")
                        yield Label("RAM GB", classes="field-label")
                        yield Label("Disk GB", classes="field-label")
                    with Horizontal(classes="trio-row"):
                        yield Input(str(b.cpu), type="integer", id="cpu")
                        yield Input(str(b.mem_gb), type="integer", id="mem")
                        yield Input(str(b.disk_gb), type="integer", id="disk")
                    yield Label("Region", classes="field-label")
                    yield Select(loading, allow_blank=False, id="region")
                    with Horizontal(classes="switch-row"):
                        yield Switch(value=b.audited_only, id="audited")
                        yield Label("audited providers only")
                    with Horizontal(classes="switch-row"):
                        yield Switch(value=True, id="fit")
                        yield Label("only providers with room now")
                    with Horizontal(classes="trio-row"):
                        yield Label("Runtime limit (hours)", classes="field-label")
                        yield Label("Price cap ($/h)", classes="field-label")
                    with Horizontal(classes="trio-row"):
                        yield Input(str(b.hours), type="integer", id="hours")
                        yield Input(f"{b.max_usd_hr:.2f}" if b.max_usd_hr else "", placeholder="from estimate", id="cap")
                    yield Label("", id="pin-line", classes="hint")
                with Vertical(id="hw-right"):
                    yield Label("", id="offers-note", classes="hint")
                    yield FilterTable([Col("", "mark", 2), Col("Provider", "name"), Col("Region", "region"), Col("Aud", "aud"), Col("Up 7d", "up", right=True),
                                       Col("Inc 7d", "inc", right=True), Col("Fit now", "fit"), Col("State", "state"), Col("Est $/h", "est", right=True), Col("Source", "src")],
                                      id="offers", empty="Loading offers…")
                    yield Static(id="hw-summary")
            yield Label("", id="err", classes="error")
            yield Label("Space mark · c compare · a check availability · Enter pin provider · u unpin · s sort · / filter · Ctrl+S use · Esc cancel", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Check availability", id="check")
                yield Button("Compare marked", id="compare")
                yield Button("Use hardware", variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#mode", RadioSet).focus()
        self._sync_mode()
        app: Any = self.app
        app.bg(app.provider.catalog, self._catalog_loaded, "Hardware catalogue", fail=self._catalog_failed)

    # ------------------------------------------------------------------ catalogue -> dropdowns
    def _catalog_failed(self, e: BaseException) -> None:
        self._catalog_loaded(hardware.HardwareCatalog(errors=[str(e)], sources={"catalogue": f"failed: {e}"}))

    def _catalog_loaded(self, cat: hardware.HardwareCatalog) -> None:
        if not self.is_attached:
            return
        self.cat = cat
        self._awaiting_catalog = False
        b = self.base
        st = self.query_one("#hw-status", Label)
        if cat.empty:
            st.update("Offline: no hardware list could be loaded (" + "; ".join(cat.errors or ["no data"]) + ").  Type vendor / model by hand: it is NOT checked against live inventory.")
        elif cat.stale:
            st.update(f"Offline: hardware list from {cat.age_text()} (may be out of date).  " + "; ".join(f"{k}: {v}" for k, v in cat.sources.items() if v != "ok"))
        else:
            st.update(f"Hardware list loaded {cat.age_text()} from {len(cat.sources) or 1} public source(s); {cat.providers_online or '?'} providers online.")
        vendors = cat.vendors()
        want_v = b.gpu_vendor or ("nvidia" if any(v == "nvidia" for v, _ in vendors) else vendors[0][0] if vendors else "")
        self._set_options(self.query_one("#g_vendor", Select), [(f"{v} · {n} model(s)", v) for v, n in vendors] or [("not stated (nvidia)", "")], want_v if want_v else "", other=True,
                  text_id="g_vendor_text", text=b.gpu_vendor)
        self._fill_models(b.gpu_model, f"{b.gpu_ram}|{b.gpu_interface}" if b.gpu_ram or b.gpu_interface else "")
        arch = [("any architecture", "")] + [(f"{a} · {n} providers", a) for a, n in sorted(cat.cpu_archs.items(), key=lambda t: -t[1])]
        self._set_options(self.query_one("#c_arch", Select), arch, b.cpu_arch)
        ven = [("any vendor", "")] + [(f"{a} · {n} providers", a) for a, n in sorted(cat.cpu_vendors.items(), key=lambda t: -t[1])]
        self._set_options(self.query_one("#c_vendor", Select), ven, b.cpu_vendor)
        for wid in ("c_vendor", "c_vendor-label"):
            self.query_one(f"#{wid}").display = bool(cat.cpu_vendors)
        miss = [f"{n} provider(s) do not state their {k.replace('_', ' ')}" for k, n in cat.unknown.items() if n and k in ("cpu_arch", "region")]
        self.query_one("#cpu-note", Label).update("; ".join(miss) + (": they are left out when you choose one." if miss else ""))
        reg = [("anywhere", "")] + [(f"{r} · {n} providers", r) for r, n in sorted(cat.regions.items(), key=lambda t: -t[1])]
        self._regions = {r for _, r in reg}
        self._set_options(self.query_one("#region", Select), reg, b.region)
        self._sync_mode()
        self._sync_other()
        self._schedule(0.05)

    def _set_options(self, sel: Select, opts: list[tuple[str, str]], want: str, *, other: bool = False, text_id: str = "", text: str = "") -> None:
        """Replace the options of a dropdown and select `want` (without firing Changed).  `other` adds a 'type it yourself' entry, selected when
        `want` is not in the list (a model nobody advertises yet can still be ordered: the order then says so)."""
        opts = list(opts) + ([("Other (type it)…", OTHER)] if other else [])
        vals = {v for _, v in opts}
        with sel.prevent(Select.Changed):
            sel.set_options(opts)
            sel.value = want if want in vals else OTHER if other and want else opts[0][1]
        if text_id:
            box = self.query_one(f"#{text_id}", Input)
            with box.prevent(Input.Changed):
                box.value = text if sel.value == OTHER else ""

    def _fill_models(self, want: str, want_variant: str = "") -> None:
        cat = self.cat
        vendor = self._vendor()
        rows = cat.models(vendor) if cat else []
        opts = [("any model of this vendor", "")]
        for r in rows:
            e = r.any
            tail = f"{e.free} free · {e.providers or '?'} provider(s)" + (f" · max {e.max_node_free}/node" if e.max_node_free else "") if e.free else "none free now"
            opts.append((f"{r.display} ({r.model}) · {tail}" if r.display.lower() != r.model else f"{r.model} · {tail}", r.model))
        self._set_options(self.query_one("#g_model", Select), opts, norm_model(want) if want else "", other=True, text_id="g_model_text", text=want)
        self._fill_variants(want_variant)

    def _fill_variants(self, want: str = "") -> None:
        cat, model = self.cat, self._model()
        opts = [("any VRAM / interface", "|")]
        if cat and model:
            row = next((r for r in cat.models(self._vendor()) if r.model == model), None)
            for e in row.variants if row else []:
                v = e.variant
                tail = f"{e.free} free" + (f" · max {e.max_node_free}/node" if e.max_node_free else "") if e.free else "none free now"
                opts.append((f"{v.ram or 'any VRAM'} · {v.interface or 'any interface'} · {tail}" + (f" · ~${e.price_avg:.2f}/GPU·h" if e.price_avg else ""), f"{v.ram}|{v.interface}"))
        self._set_options(self.query_one("#g_variant", Select), opts, want or "|")

    def _sync_mode(self) -> None:
        gpu = self._gpu_mode()
        self.query_one("#gpu-box").display = gpu
        self._sync_other()

    def _sync_other(self) -> None:
        self.query_one("#g_vendor_text").display = self._sel("g_vendor") == OTHER
        self.query_one("#g_model_text").display = self._sel("g_model") == OTHER

    # ------------------------------------------------------------------ reading the form
    def _gpu_mode(self) -> bool:
        return self.query_one("#mode", RadioSet).pressed_index == 1

    def _sel(self, wid: str) -> str:
        v = self.query_one(f"#{wid}", Select).value
        return "" if v is Select.NULL else str(v)

    def _vendor(self) -> str:
        v = self._sel("g_vendor")
        return self.query_one("#g_vendor_text", Input).value.strip().lower() if v == OTHER else v

    def _model(self) -> str:
        m = self._sel("g_model")
        return norm_model(self.query_one("#g_model_text", Input).value) if m == OTHER else m

    def _int(self, wid: str) -> int:
        try:
            return int(self.query_one(f"#{wid}", Input).value or 0)
        except ValueError:
            return 0

    def _spec(self) -> Spec:
        gpu = self._gpu_mode()
        try:
            cap = float(self.query_one("#cap", Input).value or 0)
        except ValueError:
            cap = 0.0
        ram, _, iface = self._sel("g_variant").partition("|")
        alt = tuple(dict.fromkeys(norm_model(x) for x in re.split(r"[,\s]+", self.query_one("#g_alt", Input).value) if x))
        model = self._model()
        return Spec(cpu=self._int("cpu"), mem_gb=self._int("mem"), disk_gb=self._int("disk"), gpu=self._int("gpu") if gpu else 0, gpu_model=model if gpu else "",
                    role=self.base.role, max_usd_hr=cap if cap else (self.quote.cap_usd_hr if self.quote else 0.0), hours=self._int("hours"), preset="",
                    gpu_vendor=self._vendor() if gpu else "", gpu_ram=ram if gpu else "", gpu_interface=iface if gpu else "", gpu_alt=alt if gpu and model else (),
                    cpu_arch=self._sel("c_arch"), cpu_vendor=self._sel("c_vendor"), region=self._sel("region"), providers=(self.pin[0],) if self.pin else (),
                    audited_only=self.query_one("#audited", Switch).value)

    def _problems(self, spec: Spec) -> list[str]:
        return [p for p in self.app.provider.problems(spec) if "price cap" not in p]  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ live updates
    @on(RadioSet.Changed, "#mode")
    def _mode_changed(self, e: RadioSet.Changed) -> None:
        self._sync_mode()
        self._schedule()

    @on(Select.Changed, "#g_vendor")
    def _vendor_changed(self, e: Select.Changed) -> None:
        self._sync_other()
        self._fill_models("")
        self._schedule()

    @on(Select.Changed, "#g_model")
    def _model_changed(self, e: Select.Changed) -> None:
        self._sync_other()
        self._fill_variants("")
        self._schedule()

    @on(Select.Changed)
    def _select_changed(self, e: Select.Changed) -> None:
        if e.select.id not in ("g_vendor", "g_model"):
            self._schedule()

    @on(Switch.Changed)
    def _switch_changed(self, e: Switch.Changed) -> None:
        self._schedule()

    @on(Input.Changed)
    def _input_changed(self, e: Input.Changed) -> None:
        if e.input.id == "cap":
            self._cap_touched = bool(e.value.strip())
        if e.input.id == "g_model_text" and self._sel("g_model") == OTHER:
            self._fill_variants("")
        if not self._awaiting_catalog:
            self._schedule()

    def _schedule(self, delay: float = 0.45) -> None:
        self.dirty = True
        if self._debounce:
            self._debounce.stop()
        self._debounce = self.set_timer(delay, self._recompute)

    def _recompute(self) -> None:
        if self._awaiting_catalog:
            return
        spec = self._spec()
        self._seq += 1
        seq = self._seq
        self._gpu_hint(spec)
        bad = self._problems(spec.with_(max_usd_hr=spec.max_usd_hr or 1.0))
        if bad:
            self.query_one("#err", Label).update(" · ".join(bad))
            self.adv, self.quote, self.verdict = [], None, None
            self._paint(spec, "Fix the highlighted problems to see offers.")
            self.dirty = False
            return
        self.query_one("#err", Label).update("")
        app: Any = self.app
        p = app.provider
        fit = self.query_one("#fit", Switch).value

        def work() -> tuple[list[Match] | None, str, Quote | None, Verdict | None, list[str]]:
            err = ""
            adv: list[Match] | None = None
            try:
                adv = p.matches(spec, capacity=fit)
            except Exception as e:   # offline with nothing cached: the order can still be placed by hand, it just is not checked
                err = str(e)
            try:
                q: Quote | None = p.quote(spec)
            except Exception:
                q = None
            capped = spec if spec.max_usd_hr else spec.with_(max_usd_hr=q.cap_usd_hr if q else 0.0)
            try:
                v: Verdict | None = p.guard(capped, 1, self.replacing) if capped.max_usd_hr else None
            except Exception as e:   # unreadable state / ledger: the answer is no
                v = Verdict(False, f"could not check the spend guard: {e}", capped.max_usd_hr * capped.hours)
            alts = hardware.alternatives(spec, p.offers(), self.cat, q.cpu_ref_hr if q else None) if adv is not None and not adv else []
            return adv, err, q, v, alts
        app.bg(work, lambda r: self._got(seq, spec, *r), "Hardware offers", fail=lambda e: self._got(seq, spec, None, str(e), None, None, []))

    def _got(self, seq: int, spec: Spec, adv: list[Match] | None, err: str, q: Quote | None, v: Verdict | None, alts: list[str]) -> None:
        if seq != self._seq or not self.is_attached:
            return
        self.dirty = False
        self.quote, self.verdict = q, v
        if q is not None and not self._cap_touched:
            cap = self.query_one("#cap", Input)
            with cap.prevent(Input.Changed):
                cap.value = f"{q.cap_usd_hr:.2f}"
        self.adv = adv or []
        if order_key(spec) != self.screen_key:
            self.screening = None
        msg = ""
        if adv is None:
            msg = f"Offers unavailable ({err or 'no answer'}): you can still use this hardware, but it is not checked against live inventory."
        elif not adv:
            what = spec.placement_label() or spec.label()
            msg = f"No provider currently advertises capacity for {what}."
            if alts:
                msg += "  Closest available: " + "; ".join(alts) + "."
            elif self.cat and not self.cat.empty:
                msg += "  Try fewer GPUs, any VRAM / interface variant, another region, or switch off the room filter."
        self.explain = msg
        self._paint(self._spec(), msg)

    def _gpu_hint(self, spec: Spec) -> None:
        hint, cat = self.query_one("#gpu-hint", Label), self.cat
        hits = [e for e in cat.gpus if hardware.variant_matches(e.variant, spec)] if cat and spec.gpu and spec.gpu_model else []
        if spec.gpu and spec.gpu_model and cat and not cat.empty and not hits:
            hint.update("Not currently advertised by any provider: an order for it is allowed, but nobody may bid.")
            return
        node = max((e.max_node_free or 0 for e in hits), default=0)
        if node:
            warn = f"  ⚠ {spec.gpu} is more than any single node has free now" if spec.gpu > node else ""
            hint.update(f"Up to {node} GPU(s) are free on one node of an audited provider right now.{warn}")
        elif hits and all(e.free is None for e in hits):
            hint.update("Supported by the network, but no provider advertises free units right now.")
        else:
            hint.update("")

    # ------------------------------------------------------------------ rendering
    def _paint(self, spec: Spec, msg: str) -> None:
        ms = self.screening.matches if self.screening else self.adv
        t = self.query_one("#offers", FilterTable)
        t.query_one("#empty", Static).update(Text(msg or "Nothing to show.", style="dim"))
        self._rows = {m.offer.id: m for m in ms}   # a pin does not hide the other providers: the choice stays comparable
        self.marked = [k for k in self.marked if k in self._rows]
        t.set_rows([match_row(m, m.offer.id in self.marked) for m in ms])
        note = getattr(self.app.provider, "offers_status", "ok")
        parts = [f"{len(ms)} offer(s)"]
        if self.screening:
            parts.append(self.screening.summary)
        elif self.screen_key is None:
            parts.append("not availability-checked yet (press a): rows are advertised capacity only")
        else:
            parts.append("order changed since the last availability check: rows are advertised capacity only")
        if note != "ok":
            parts.append(f"provider list {note}")
        parts.append("prices are market estimates, identical for every provider until bids exist")
        self.query_one("#offers-note", Label).update(" · ".join(parts))
        self.query_one("#pin-line", Label).update(f"Pinned provider: {self.pin[1]}.  Only its bids are accepted; if it does not bid, the order is closed and no lease is made." if self.pin else "")
        self.query_one("#hw-summary", Static).update(self._summary_table(spec))

    def _summary_table(self, spec: Spec) -> Table:
        q, v = self.quote, self.verdict
        ceil = hardware.ceiling(spec)
        rows: list[tuple[str, Any]] = [("Order", Text(spec.label() + ((" · " + spec.placement_label()) if spec.placement_label() else "")))]
        rows.append(("Cost ceiling", Text(f"cap {fmt.rate(spec.max_usd_hr)} × {spec.hours} h = {fmt.money(ceil)}  (the most it can cost)", style="bold yellow")))
        if q is not None:
            band = f"  (GPU range {fmt.rate(q.gpu_low_hr)}–{fmt.rate(q.gpu_high_hr)})" if q.gpu_low_hr is not None and q.gpu_high_hr is not None else ""
            rows.append(("Estimate", Text(f"~{fmt.rate(q.usd_hr)} · {q.source or 'estimate'} · unverified{band}")))
        else:
            rows.append(("Estimate", Text("unknown", style="dim")))
        if v is not None:
            rows.append(("Spend guard", Text(("OK  " if v.ok else "BLOCKED  ") + v.text, style="green" if v.ok else "bold red")))
        rows.append(("", Text(DISCLAIMER, style="dim")))
        return kv_table(rows)

    # ------------------------------------------------------------------ actions
    def _current_match(self) -> Match | None:
        key = self.query_one("#offers", FilterTable).selected_key
        return self._rows.get(key) if key else None

    def action_mark(self) -> None:
        if not self.query_one("#offers", FilterTable).table.has_focus:   # Space is a normal character in the text fields
            return
        m = self._current_match()
        if not m:
            return
        k = m.offer.id
        if k in self.marked:
            self.marked.remove(k)
        elif len(self.marked) >= MAX_MARKS:
            self.app.notify(f"Compare up to {MAX_MARKS} offers: unmark one first.", severity="warning", timeout=4)
            return
        else:
            self.marked.append(k)
        self._paint(self._spec(), self.explain)

    def action_compare(self) -> None:
        picks = [self._rows[k] for k in self.marked if k in self._rows]
        if len(picks) < 2:
            self.app.notify("Mark 2-3 offers with Space first.", severity="warning", timeout=4)
            return
        self.app.push_screen(CompareScreen(picks), self._pinned_from_compare)

    def _pinned_from_compare(self, owner: str | None) -> None:
        m = self._rows.get(owner or "")
        if m:
            self._do_pin(m)

    @on(FilterTable.Selected)
    def _row_selected(self, e: FilterTable.Selected) -> None:
        e.stop()
        m = self._rows.get(e.key)
        if m:
            self._do_pin(m)

    def _do_pin(self, m: Match) -> None:
        self.pin = (m.offer.id, f"{m.offer.name} ({short(m.offer.id)})")
        reg = self.query_one("#region", Select)
        if m.offer.region_code and not self._sel("region") and m.offer.region_code in self._regions:   # pre-fill the region the provider states
            with reg.prevent(Select.Changed):
                reg.value = m.offer.region_code
        self._paint(self._spec(), self.explain)
        self.app.notify(f"Pinned {m.offer.name}: only its bids are accepted.", timeout=4)

    def action_unpin(self) -> None:
        if self.pin:
            self.pin = None
            self._schedule(0.05)

    def action_sort(self) -> None:
        self.query_one("#offers", FilterTable).cycle_sort()

    def action_check(self) -> None:
        """The explicit availability check: bid-screening for exactly this order (capacity + attributes, unpriced).  Never deploys."""
        spec = self._spec()
        if self._checking:
            return
        bad = self._problems(spec.with_(max_usd_hr=spec.max_usd_hr or 1.0))
        if bad:
            self.query_one("#err", Label).update(" · ".join(bad))
            return
        app: Any = self.app
        if not app.provider.has("screen"):
            app.notify(f"{app.provider.label} cannot check availability.", severity="warning")
            return
        self._checking = True
        self.query_one("#check", Button).disabled = True
        self.query_one("#offers-note", Label).update("Checking availability with the network…")

        def done(s: Screening) -> None:
            self._checking = False
            if self.is_attached:
                self.query_one("#check", Button).disabled = False
                self.screening, self.screen_key = s, order_key(spec)
                self._paint(self._spec(), self.explain)

        def failed(e: BaseException) -> None:
            done(Screening(self.adv, ok=False, error=str(e)))
        app.bg(lambda: app.provider.screen(spec), done, "Availability check", fail=failed)

    def action_accept(self) -> None:
        spec = self._spec()
        bad = self._problems(spec)
        if bad:
            self.query_one("#err", Label).update(" · ".join(bad))
            return
        if self.verdict is not None and not self.verdict.ok and spec.max_usd_hr:
            self.query_one("#err", Label).update("Refused by the spend guard: " + self.verdict.text)
            return
        self.dismiss(spec)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#check")
    def _b_check(self) -> None:
        self.action_check()

    @on(Button.Pressed, "#compare")
    def _b_compare(self) -> None:
        self.action_compare()

    @on(Button.Pressed, "#ok")
    def _b_ok(self) -> None:
        self.action_accept()

    @on(Button.Pressed, "#cancel")
    def _b_cancel(self) -> None:
        self.dismiss(None)
