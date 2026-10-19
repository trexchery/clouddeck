"""The hardware picker in the real app (Textual pilot, simulated provider: nothing real is touched): dropdowns built from the catalogue, offers with
their evidence labels, the explicit availability check, compare / pin, the cost ceiling and guard verdict before anything is confirmed, offline and
empty states, keyboard use.  Waits are on state (the picker's `dirty` flag, the app's busy counter), never on fixed sleeps."""
import asyncio
import time

from textual.widgets import Button, DataTable, Input, RadioButton, Select, Switch

from clouddeck import providers
from clouddeck.providers.demo import DemoProvider
from clouddeck.tui.app import CloudDeckApp
from clouddeck.tui.compute import DeployScreen
from clouddeck.tui.picker import OTHER, CompareScreen, HardwareScreen
from clouddeck.tui.widgets import ConfirmScreen, FilterTable, TaskScreen

WAIT = 30.0


def txt(obj):
    obj = getattr(obj, "content", obj) if not isinstance(obj, str) else obj
    if isinstance(obj, str):
        return obj
    if hasattr(obj, "plain"):
        return obj.plain
    import io

    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=220, color_system=None).print(obj)
    return buf.getvalue()


async def wait_for(pred, pilot, what="the expected state", timeout=WAIT):
    end = time.monotonic() + timeout
    err = None
    while True:
        try:
            if pred():
                return
            err = None
        except Exception as e:   # a widget that is not mounted yet is "not ready", not a failure
            err = e
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what} (screen: {type(pilot.app.screen).__name__}" + (f"; last error: {err!r})" if err else ")"))
        await pilot.pause(0.02)


def drive(fn, provider=None, size=(160, 48)):
    provider = provider or DemoProvider(latency=0)

    async def go():
        app = CloudDeckApp(provider, poll_s=0.5, net_s=1.0, probe_s=1.0, money_s=1.0)
        async with app.run_test(size=size) as pilot:
            await wait_for(lambda: len(app.instances) >= 5 and app.spend is not None and isinstance(app.focused, DataTable), pilot, "the first data and the focus")
            await fn(app, pilot, provider)
    asyncio.run(go())
    return provider


def calls(p, name):
    return [c[1] for c in p.calls if c[0] == name]


def flat(obj):
    """Text of a widget with the line breaks of the screen taken out (a sentence may wrap)."""
    return " ".join(txt(obj).split())


def opts(sel):
    return [v for _, v in sel._options]


def labels(sel):
    return [txt(p) for p, _ in sel._options]


async def picker(app, pilot, key="h"):
    await pilot.press(key)
    await wait_for(lambda: isinstance(app.screen, HardwareScreen) and app.screen.cat is not None, pilot, "the picker with its catalogue")
    return app.screen


async def settle(pilot, app, s):
    """The offers on screen belong to the values now in the form: the debounce fired and the background work finished."""
    await pilot.pause()                                                                      # let the queued Changed messages run first: they are what mark the form dirty
    await wait_for(lambda: not s.dirty and app.busy == 0, pilot, "the offers for the form's values")


async def gpu_mode(pilot, app, s):
    s.query_one("#m-gpu", RadioButton).value = True
    await wait_for(lambda: s.query_one("#gpu-box").display, pilot, "GPU mode")
    await settle(pilot, app, s)


async def pick_gpu(pilot, app, s, model, variant="|", count="1"):
    await gpu_mode(pilot, app, s)
    s.query_one("#g_model", Select).value = model
    await wait_for(lambda: variant in opts(s.query_one("#g_variant", Select)), pilot, "the variants of that model")
    s.query_one("#g_variant", Select).value = variant
    s.query_one("#gpu", Input).value = count
    await settle(pilot, app, s)


def rows(s):
    t = s.query_one("#offers", FilterTable)
    return t, [t.table.get_row_at(i) for i in range(t.table.row_count)]


# ------------------------------------------------------------------ the dropdowns come from the catalogue
def test_picker_opens_and_its_dropdowns_are_built_from_the_catalogue():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        assert "Hardware list loaded" in txt(s.query_one("#hw-status")) and not s.query_one("#gpu-box").display          # CPU mode first
        assert opts(s.query_one("#c_arch", Select)) == ["", "x86-64", "arm-64"] and "arm-64 · 1 providers" in labels(s.query_one("#c_arch", Select))
        assert {"", "intel", "amd", "arm"} == set(opts(s.query_one("#c_vendor", Select))) and "eu-north" in opts(s.query_one("#region", Select))
        await gpu_mode(pilot, app, s)
        vendors = opts(s.query_one("#g_vendor", Select))
        assert vendors[:2] == ["nvidia", "amd"] and vendors[-1] == OTHER                                                        # AMD and a model nobody lists are here only because the catalogue says so
        models = opts(s.query_one("#g_model", Select))
        assert {"a100", "h100", "v100", "rtx4090", "zx9000", "b300"} <= set(models) and models[0] == "" and models[-1] == OTHER
        assert any("none free now" in x for x in labels(s.query_one("#g_model", Select)))                                       # supported but not offered: shown, marked
        s.query_one("#g_vendor", Select).value = "amd"
        await wait_for(lambda: "mi100" in opts(s.query_one("#g_model", Select)), pilot, "the AMD models")
        assert set(opts(s.query_one("#g_model", Select))) >= {"", "mi100", "mi60", OTHER} and "a100" not in opts(s.query_one("#g_model", Select))
    drive(t)


def test_vram_and_interface_variants_are_choosable_per_model():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await gpu_mode(pilot, app, s)
        s.query_one("#g_model", Select).value = "a100"
        await wait_for(lambda: "80Gi|sxm" in opts(s.query_one("#g_variant", Select)), pilot, "the a100 variants")
        assert opts(s.query_one("#g_variant", Select)) == ["|", "80Gi|sxm", "40Gi|pcie"]                                        # 'any' first, then by free units
        assert "~$1.49/GPU·h" in " ".join(labels(s.query_one("#g_variant", Select)))
        s.query_one("#g_variant", Select).value = "40Gi|pcie"
        await settle(pilot, app, s)
        spec = s._spec()
        assert (spec.gpu_model, spec.gpu_ram, spec.gpu_interface, spec.gpu_vendor) == ("a100", "40Gi", "pcie", "nvidia")
        s.query_one("#g_model", Select).value = "rtx4090"                                                                       # changing the model resets the variant
        await wait_for(lambda: opts(s.query_one("#g_variant", Select)) == ["|", "24Gi|pcie"], pilot, "the rtx4090 variants")
        assert s._spec().gpu_ram == ""
    drive(t)


def test_more_gpus_than_one_node_has_is_a_warning_not_an_error():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "v100", count="4")
        assert "Up to 4 GPU(s)" in txt(s.query_one("#gpu-hint")) and "⚠" not in txt(s.query_one("#gpu-hint"))
        s.query_one("#gpu", Input).value = "5"
        await settle(pilot, app, s)
        assert "more than any single node" in txt(s.query_one("#gpu-hint")) and not txt(s.query_one("#err")).strip()
    drive(t)


def test_a_model_no_list_knows_can_be_typed_and_is_flagged():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await gpu_mode(pilot, app, s)
        s.query_one("#g_model", Select).value = OTHER
        await wait_for(lambda: s.query_one("#g_model_text").display, pilot, "the text box for the model")
        s.query_one("#g_model_text", Input).value = "ZZ 9999"
        await settle(pilot, app, s)
        assert s._spec().gpu_model == "zz9999" and "Not currently advertised" in txt(s.query_one("#gpu-hint"))
        t_, r = rows(s)
        assert r == [] and "No provider currently advertises" in txt(t_.query_one("#empty"))
    drive(t)


# ------------------------------------------------------------------ offers: advertised, screened (only on request), filters, compare, pin
def test_offers_are_advertised_until_the_user_asks_the_network_to_check():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        t_, r = rows(s)
        assert len(r) == 6 and all("advertised" in txt(row[7]) for row in r) and not calls(p, "screen")
        assert "not availability-checked yet" in txt(s.query_one("#offers-note")) and "estimates" in txt(s.query_one("#offers-note"))
        s.query_one("#cpu", Input).value = "16"                                                                                  # editing the form never asks the network by itself
        await settle(pilot, app, s)
        assert not calls(p, "screen")
        await pilot.click("#check")
        await wait_for(lambda: s.screening is not None, pilot, "the availability check")
        await settle(pilot, app, s)
        assert len(calls(p, "screen")) == 1 and calls(p, "screen")[0].cpu == 16
        _, r = rows(s)
        states = [txt(row[7]) for row in r]
        assert states.count("● screened") == 5 and states.count("◌ advertised") == 1 and "screened 5 provider(s)" in txt(s.query_one("#offers-note"))
        s.query_one("#cpu", Input).value = "8"                                                                                   # a changed order invalidates the check
        await settle(pilot, app, s)
        assert s.screening is None and all("advertised" in txt(row[7]) for row in rows(s)[1]) and "order changed" in txt(s.query_one("#offers-note"))
    drive(t)


def test_offers_show_provider_region_free_capacity_and_a_labelled_estimate():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "rtx4090")
        _, r = rows(s)
        assert len(r) == 1
        cells = [txt(c) for c in r[0]]
        assert cells[1] == "charlie" and cells[2] == "na-us-southeast" and cells[3] == "yes" and "3 GPU" in cells[6] and cells[7] == "◌ advertised"
        assert cells[8].startswith("~$") and cells[9] in ("own history", "market avg")                                           # an estimate, with where it comes from
        assert "confirmed" not in " ".join(cells)                                                                                # nothing here is a confirmed price
    drive(t)


def test_filter_search_and_sort_work_on_the_offers():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        t_ = s.query_one("#offers", FilterTable)
        t_.focus_table()
        await pilot.press("slash")
        await pilot.press(*"echo")
        await wait_for(lambda: t_.table.row_count == 1, pilot, "the filtered table")
        await pilot.press("escape")
        await wait_for(lambda: t_.table.row_count == 6, pilot, "the full table")
        s.query_one("#c_arch", Select).value = "arm-64"                                                                          # form filters: arm only, and nothing has room for 8 vCPU here
        await settle(pilot, app, s)
        assert t_.table.row_count == 1 and txt(rows(s)[1][0][1]) == "foxtrot"
        s.query_one("#audited", Switch).value = True
        await settle(pilot, app, s)
        assert t_.table.row_count == 0 and "No provider currently advertises" in txt(t_.query_one("#empty"))
        assert "Closest available" in txt(t_.query_one("#empty"))                                                                # and it says what would work
    drive(t)


def test_mark_two_or_three_offers_compare_them_and_pin_one():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        t_ = s.query_one("#offers", FilterTable)
        t_.focus_table()
        await pilot.press("space")
        await pilot.press("down", "space")
        await wait_for(lambda: len(s.marked) == 2, pilot, "two marked offers")
        assert [txt(r[0]).strip() for r in rows(s)[1]].count("●") == 2
        await pilot.press("down", "space", "down", "space")                                                                      # a 4th mark is refused, the first three stay
        await wait_for(lambda: len(s.marked) == 3, pilot, "three marked offers")
        await pilot.pause(0.1)
        assert len(s.marked) == 3
        await pilot.press("up", "space")                                                                                         # on a marked row: unmark
        await wait_for(lambda: len(s.marked) == 2, pilot, "one unmarked")
        await pilot.press("c")
        await wait_for(lambda: isinstance(app.screen, CompareScreen), pilot, "the compare screen")
        body = txt(app.screen.query_one("#cmp"))
        assert all(name in body for name in (s._rows[k].offer.name for k in s.marked)) and "Estimate" in body and "Uptime 7d" in body and "No price here is a bid" in txt(app.screen.query_one(".hint"))
        chosen = app.screen.ms[0].offer
        await pilot.click("#pick-0")
        await wait_for(lambda: app.screen is s, pilot, "back in the picker")
        assert s.pin and s.pin[0] == chosen.id and chosen.name in txt(s.query_one("#pin-line"))
        assert s._spec().providers == (chosen.id,)
        await pilot.press("u")                                                                                                   # unpin
        await settle(pilot, app, s)
        assert s.pin is None and s._spec().providers == ()
    drive(t)


def test_compare_needs_two_marks():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        s.query_one("#offers", FilterTable).focus_table()
        await pilot.press("space", "c")
        await pilot.pause(0.2)
        assert isinstance(app.screen, HardwareScreen)
    drive(t)


def test_enter_on_an_offer_pins_it_and_pre_fills_its_region():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        s.query_one("#offers", FilterTable).focus_table()
        await pilot.press("down", "down", "enter")
        await wait_for(lambda: s.pin is not None, pilot, "the pin")
        await settle(pilot, app, s)
        o = s._rows[s.pin[0]].offer
        assert s._spec().providers == (o.id,) and s._spec().region == o.region_code
        assert "Only its bids are accepted" in txt(s.query_one("#pin-line")) and "no lease is made" in txt(s.query_one("#pin-line"))
    drive(t)


# ------------------------------------------------------------------ before anything is confirmed: ceiling, guard, disclaimer
def test_the_cost_ceiling_the_guard_and_the_disclaimer_are_on_screen_before_any_confirm():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "a100", "80Gi|sxm", count="2")
        s.query_one("#cap", Input).value = "3.00"
        s.query_one("#hours", Input).value = "5"
        await settle(pilot, app, s)
        body = flat(s.query_one("#hw-summary"))
        assert "cap $3.000/h × 5 h = $15.00" in body                                                                             # price cap x runtime hours
        assert "Spend guard" in body and "OK" in body and "Estimate" in body and "market-average" in body and "unverified" in body
        assert "upper bound, not a promise" in body and "confirmed price is the bid you get after ordering" in body
        s.query_one("#cap", Input).value = "9.00"
        p.limits["max_usd_hr_hard"] = 5.0                                                                                        # a ceiling below it: the verdict says so right here
        await settle(pilot, app, s)
        assert "BLOCKED" in txt(s.query_one("#hw-summary")) and "max_usd_hr_hard" in txt(s.query_one("#hw-summary"))
        s.action_accept()
        assert isinstance(app.screen, HardwareScreen) and "Refused by the spend guard" in txt(s.query_one("#err"))              # and it cannot be used
        assert not calls(p, "deploy")
    drive(t)


def test_choosing_hardware_fills_the_deploy_dialog_and_the_controller_gets_exactly_that_spec():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "a100", "80Gi|sxm", count="2")
        s.query_one("#c_arch", Select).value = "x86-64"
        s.query_one("#region", Select).value = "eu-north"
        s.query_one("#audited", Switch).value = True
        s.query_one("#cap", Input).value = "3.50"
        s.query_one("#hours", Input).value = "3"
        await settle(pilot, app, s)
        s.query_one("#offers", FilterTable).focus_table()
        await pilot.press("enter")                                                                                               # pin the (only) matching provider
        await wait_for(lambda: s.pin is not None, pilot, "the pin")
        await settle(pilot, app, s)
        await pilot.press("ctrl+s")
        await wait_for(lambda: isinstance(app.screen, DeployScreen), pilot, "the deploy dialog")
        d = app.screen
        assert d.query_one("#gpu", Input).value == "2" and d.query_one("#gpu_model", Select).value == "a100" and d.query_one("#hours", Input).value == "3"
        assert d.query_one("#cap", Input).value == "3.50" and "a100" in txt(d.query_one("#placement")) and "eu-north" in txt(d.query_one("#placement"))
        await wait_for(lambda: d.quote is not None and d.verdict is not None and "Placement" in txt(d.query_one("#quote")), pilot, "the estimate for the chosen hardware")
        await wait_for(lambda: d.query_one("#ok").region.width > 0, pilot, "the OK button")
        await pilot.click("#ok")
        await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.query("#ok"), pilot, "the review")
        body = flat(app.screen.query_one("#dialog-body"))
        assert "Cost ceiling" in body and "cap $3.500/h × 3 h = $10.50" in body and "Provider pin" in body and "Spend guard" in body and "not bids" in body
        await wait_for(lambda: app.screen.query_one("#ok").region.width > 0, pilot, "the confirm button")
        await pilot.click("#ok")
        await wait_for(lambda: calls(p, "deploy"), pilot, "the deploy call")
        spec = calls(p, "deploy")[0]
        assert (spec.gpu, spec.gpu_model, spec.gpu_ram, spec.gpu_interface, spec.cpu_arch, spec.region, spec.audited_only, spec.hours, spec.max_usd_hr) == \
            (2, "a100", "80Gi", "sxm", "x86-64", "eu-north", True, 3, 3.5)
        assert len(spec.providers) == 1
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task")
    drive(t)


def test_choosing_a_preset_afterwards_drops_the_placement():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "a100", "80Gi|sxm", count="1")
        s.query_one("#region", Select).value = "eu-north"
        await settle(pilot, app, s)
        await pilot.press("ctrl+s")
        d = await _deploy_dialog(app, pilot)
        assert "eu-north" in txt(d.query_one("#placement"))
        d.query_one("#preset", Select).value = "16"
        await wait_for(lambda: txt(d.query_one("#placement")) == "" and d.ext["region"] == "", pilot, "the preset to replace the placement")
        assert d._spec().region == "" and d._spec().cpu == 16 and d._spec().preset == "16"
    drive(t)


async def _deploy_dialog(app, pilot):
    await wait_for(lambda: isinstance(app.screen, DeployScreen) and app.screen.query("#cpu") and app.screen.query("#hw"), pilot, "the deploy dialog")
    return app.screen


def test_the_deploy_dialog_has_a_hardware_button_and_key():
    async def t(app, pilot, p):
        await pilot.press("d")
        d = await _deploy_dialog(app, pilot)
        assert d.query_one("#hw", Button)
        await pilot.press("f2")
        await wait_for(lambda: isinstance(app.screen, HardwareScreen) and app.screen.cat is not None, pilot, "the picker")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is d, pilot, "back in the deploy dialog")
        await pilot.press("escape")
        assert not calls(p, "deploy")
    drive(t)


def test_a_provider_row_opens_the_picker_pinned_to_it():
    async def t(app, pilot, p):
        await pilot.press("p")
        t_ = app.query_one("#prov", FilterTable)
        await wait_for(lambda: t_.table.row_count == 6, pilot, "the providers")
        await pilot.press("enter")
        await wait_for(lambda: isinstance(app.screen, HardwareScreen) and app.screen.cat is not None, pilot, "the picker")
        assert app.screen.pin and app.screen.base.providers == (app.screen.pin[0],)
        await pilot.press("escape")
        assert not calls(p, "deploy")
    drive(t)


def test_the_command_palette_lists_the_picker():
    async def t(app, pilot, p):
        titles = [c.title for c in app.get_system_commands(app.screen)]
        assert "Browse hardware…" in titles
    drive(t)


# ------------------------------------------------------------------ empty, offline and stale states
def test_nothing_available_says_why_and_what_would_work():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "a100", count="8")
        s.query_one("#region", Select).value = "eu-central"                                                                      # the only provider there has no GPUs
        await settle(pilot, app, s)
        t_, r = rows(s)
        msg = flat(t_.query_one("#empty"))
        assert r == [] and "No provider currently advertises capacity for" in msg and "Closest available" in msg and "advertise capacity for any region" in msg
        assert not txt(s.query_one("#err")).strip()
        assert "Spend guard" in txt(s.query_one("#hw-summary"))                                                                  # the rest of the screen keeps working
    drive(t)


def test_offline_the_picker_says_so_and_manual_entry_still_deploys():
    p = DemoProvider(latency=0, hw_state="down")

    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        assert "Offline: no hardware list could be loaded" in txt(s.query_one("#hw-status")) and "NOT checked against live inventory" in txt(s.query_one("#hw-status"))
        t_, r = rows(s)
        assert r == [] and "Offers unavailable" in txt(t_.query_one("#empty")) and "not checked against live inventory" in txt(t_.query_one("#empty"))
        await gpu_mode(pilot, app, s)
        s.query_one("#g_model", Select).value = OTHER
        await wait_for(lambda: s.query_one("#g_model_text").display, pilot, "the text box")
        s.query_one("#g_model_text", Input).value = "rtx4090"
        s.query_one("#gpu", Input).value = "1"
        await settle(pilot, app, s)
        assert "Spend guard" in txt(s.query_one("#hw-summary")) and "Cost ceiling" in txt(s.query_one("#hw-summary"))          # the safety side does not need the network lists
        await pilot.press("ctrl+s")
        d = await _deploy_dialog(app, pilot)
        await wait_for(lambda: d.quote is not None and d.verdict is not None, pilot, "the estimate")
        await wait_for(lambda: d.query_one("#ok").region.width > 0, pilot, "the OK button")
        await pilot.click("#ok")
        await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.query("#ok"), pilot, "the review")
        await wait_for(lambda: app.screen.query_one("#ok").region.width > 0, pilot, "the confirm button")
        await pilot.click("#ok")
        await wait_for(lambda: calls(p, "deploy"), pilot, "the deploy call")
        assert calls(p, "deploy")[0].gpu_model == "rtx4090"
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task")
    drive(t, provider=p)


def test_a_stale_catalogue_says_how_old_it_is():
    p = DemoProvider(latency=0, hw_state="stale")

    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        assert "Offline: hardware list from 3.0 h ago" in txt(s.query_one("#hw-status")) and "may be out of date" in txt(s.query_one("#hw-status"))
        assert s.query_one("#m-cpu", RadioButton)
    drive(t, provider=p)


def test_checking_availability_offline_is_a_message_and_the_rows_stay():
    p = DemoProvider(latency=0)
    p.screen = lambda spec: __import__("clouddeck.hardware", fromlist=["Screening"]).Screening(p.matches(spec), at=time.time(), ok=False, error="network unreachable")

    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        await pilot.click("#check")
        await wait_for(lambda: s.screening is not None, pilot, "the answer")
        await settle(pilot, app, s)
        assert "screening unavailable (network unreachable)" in txt(s.query_one("#offers-note")) and len(rows(s)[1]) == 6
        assert all("advertised" in txt(r[7]) for r in rows(s)[1]) and not s.query_one("#check", Button).disabled
    drive(t, provider=p)


# ------------------------------------------------------------------ keyboard, layout
def test_every_control_can_be_reached_with_the_keyboard():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await gpu_mode(pilot, app, s)
        controls = [w for w in s.query("Input, Select, Switch, Button, RadioSet, DataTable") if w.display and w.id not in ("q",)]
        assert len(controls) >= 14 and all(w.can_focus for w in controls), [w.id for w in controls if not w.can_focus]
        seen = set()
        for _ in range(40):                                                                                                      # Tab visits them all and comes back around
            await pilot.press("tab")
            if app.focused is not None:
                seen.add(app.focused.id)
        assert {"g_vendor", "g_model", "g_variant", "gpu", "c_arch", "cpu", "mem", "disk", "region", "audited", "fit", "hours", "cap", "t", "check", "compare", "ok", "cancel"} <= seen, seen
        await pilot.press("escape")
        await wait_for(lambda: not isinstance(app.screen, HardwareScreen), pilot, "the picker to close")
    drive(t)


def test_the_mode_switch_works_from_the_keyboard():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        assert app.focused is s.query_one("#mode")
        await pilot.press("right", "space")
        await wait_for(lambda: s.query_one("#gpu-box").display, pilot, "GPU mode from the keyboard")
        await pilot.press("left", "space")
        await wait_for(lambda: not s.query_one("#gpu-box").display, pilot, "CPU mode again")
    drive(t)


def test_narrow_and_short_terminals_stay_usable():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await settle(pilot, app, s)
        assert s.query_one("#form").region.width > 0 and s.query_one("#offers").region.width > 0 and s.query_one("#hw-summary").region.width > 0
        assert s.query_one("#ok").region.height > 0 and s.query_one("#cancel").region.width > 0
        await gpu_mode(pilot, app, s)
        await pilot.press("escape")
    drive(t, size=(70, 28))


def test_cancel_returns_nothing_and_changes_nothing():
    async def t(app, pilot, p):
        s = await picker(app, pilot)
        await pick_gpu(pilot, app, s, "a100", count="1")
        await pilot.click("#cancel")
        await wait_for(lambda: not isinstance(app.screen, HardwareScreen), pilot, "the picker to close")
        assert not isinstance(app.screen, DeployScreen) and not calls(p, "deploy") and not calls(p, "screen")
    drive(t)


def test_the_picker_works_through_the_provider_registry_too():
    assert providers.get("demo").has("hardware")


def test_the_instance_detail_shows_the_confirmed_bids():
    p = DemoProvider(latency=0)
    inst = next(i for i in p._items.values() if i.role == "worker")
    inst.meta["bids"] = [{"provider": "akash1x", "host": "a.example", "usd_hr": 0.3, "speed": 1.0}, {"provider": "akash1y", "host": "b.example", "usd_hr": 0.41, "speed": 0.9}]

    async def t(app, pilot, p):
        tbl = app.query_one("#inst", FilterTable)
        await wait_for(lambda: inst.id in tbl._shown, pilot, "the instance row")
        tbl.table.move_cursor(row=tbl._shown.index(inst.id))
        await wait_for(lambda: "Bids received" in txt(app.query_one("#inst-detail")), pilot, "the instance detail")
        body = flat(app.query_one("#inst-detail"))
        assert "confirmed: the winning bid" in body and "a.example $0.300/h" in body and "b.example $0.410/h" in body
    drive(t, provider=p)
