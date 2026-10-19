"""End-to-end TUI tests: the real app, real key presses and clicks, the simulated provider (nothing real is touched).

Nothing in here sleeps for a fixed time.  Every wait is a condition with a generous timeout (`wait_for`): a fast machine never pays the
timeout, a loaded one gets the time it needs.  Negative checks ("nothing was started") use the app's own counters, which are updated
synchronously when something starts, so they do not depend on timing either."""
import ast
import asyncio
import time
from pathlib import Path

import pytest
from textual.app import App
from textual.widgets import DataTable, Input, Select

from clouddeck import providers
from clouddeck.engine import ddc
from clouddeck.providers.base import Spec
from clouddeck.tui.app import CloudDeckApp, describe
from clouddeck.tui.compute import DeployScreen
from clouddeck.tui.widgets import ConfirmScreen, FilterTable, FormScreen, SelectScreen, TaskScreen
from clouddeck.tui.workers import DispatchScreen

TABS = dict(zip("12345", ["compute", "workers", "projects", "monitor", "recovery"]))
WAIT = 90.0   # a ceiling for a loaded machine (a step normally takes milliseconds); nothing waits this long when things work


def txt(obj):
    """Plain text of whatever a widget renders (str, Rich Text/Table, Textual Content)."""
    obj = getattr(obj, "content", obj) if not isinstance(obj, str) else obj
    if isinstance(obj, str):
        return obj
    if hasattr(obj, "plain"):
        return obj.plain
    import io

    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=200, color_system=None).print(obj)
    return buf.getvalue()


# ------------------------------------------------------------------ waiting helpers
async def wait_for(pred, pilot, what="the expected state", timeout=WAIT):
    end = time.monotonic() + timeout
    err = None
    while True:
        try:
            if pred():
                return
            err = None
        except Exception as e:    # a widget that is not mounted yet is "not ready", not a failure
            err = e
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what} (screen: {type(pilot.app.screen).__name__}, focus: {pilot.app.focused!r}" + (f"; last error: {err!r})" if err else ")"))
        await pilot.pause(0.02)


async def quiet(pilot, app):
    """Every background call and task the UI started has finished and delivered its result (`busy` drops only after the callback ran)."""
    await wait_for(lambda: app.busy == 0, pilot, "background work to finish")
    await pilot.pause()


def nothing_started(app):
    """No task window was opened: `run_task` registers it synchronously at the moment it starts.  (`busy` is no use here: the app's own background
    work, such as the first market load or a probe, counts in it and takes as long as the machine needs.)"""
    return not app._tasks and not any(isinstance(s, TaskScreen) for s in app.screen_stack)


async def screen_is(pilot, app, cls):
    await wait_for(lambda: isinstance(app.screen, cls), pilot, f"the {cls.__name__} to open")
    return app.screen


async def goto(pilot, app, key):
    """Switch tab with its number key and wait until the focus is inside it (key bindings only work from there)."""
    tab = TABS[key]
    await pilot.press(key)
    await wait_for(lambda: app.query_one("#tabs").active == tab and app.focused is not None and tab in {getattr(a, "id", None) for a in app.focused.ancestors},
                   pilot, f"the {tab} tab with the focus inside it")


async def focus_in_pane(pilot, app):
    await wait_for(lambda: app.screen is app.screen_stack[0] and app.focused is not None and app.active_pane() in app.focused.ancestors, pilot, "the focus back in the open tab")


async def selected(pilot, app, table, key):
    """Put the table's cursor on the row with this key."""
    tbl = app.query_one(f"#{table}", FilterTable)
    await wait_for(lambda: key in tbl._shown, pilot, f"row {key} in {table}")
    tbl.table.move_cursor(row=tbl._shown.index(key))
    await wait_for(lambda: tbl.selected_key == key, pilot, f"row {key} selected")


async def ok(pilot, app):
    """Click the dialog's button once it is laid out and enabled (a click on a widget that has no size yet is lost)."""
    await wait_for(lambda: (b := app.screen.query_one("#ok")).region.width > 0 and not b.disabled, pilot, "the dialog's OK button")
    await pilot.click("#ok")


async def confirm_open(pilot, app):
    """The review dialog of deploy / resize / scale-up opens once the spend guard has answered (it runs off the UI thread)."""
    await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.query("#ok"), pilot, "a confirmation dialog")
    return app.screen


async def typed(pilot, app, word):
    """A destructive dialog: the button stays disabled until the phrase is typed exactly."""
    s = await confirm_open(pilot, app)
    assert s.word == word and s.query_one("#ok").disabled
    s.query_one("#word", Input).value = word
    await ok(pilot, app)                                                      # waits for the typed phrase to enable the button
    await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")


async def close_task(pilot, app):
    """Wait for the task window to show its result, then close it."""
    await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to finish")
    await pilot.click("#hide")
    await wait_for(lambda: not isinstance(app.screen, TaskScreen), pilot, "the task window to close")


async def quote_for(pilot, s):
    """The estimate on screen belongs to the values now in the form: the debounce fired and the answer (estimate and guard verdict) arrived."""
    await wait_for(lambda: s.quote is not None and s.verdict is not None and s._spec().label() in txt(s.query_one("#quote")), pilot, "the estimate for the form's values")


def drive(fn, size=(150, 45), provider=None):
    """Run `fn(app, pilot, provider)` against a live app and return the provider for assertions."""
    provider = provider or providers.get("demo")

    async def go():
        app = CloudDeckApp(provider, poll_s=0.5, net_s=1.0, probe_s=1.0, money_s=1.0)
        async with app.run_test(size=size) as pilot:
            await wait_for(lambda: len(app.instances) >= len(provider.instances()) and app.spend is not None and isinstance(app.focused, DataTable), pilot, "the first data and the focus")
            await quiet(pilot, app)                                  # the first market / workspace loads are done: tests start from a still app
            await fn(app, pilot, provider)
            assert not app.ui_errors, app.ui_errors                  # a callback that raised on the UI thread would otherwise vanish silently
    asyncio.run(go())
    return provider


def calls(p, name):
    return [c[1] for c in p.calls if c[0] == name]


def palette_run(app, title):
    """Run a command-palette entry exactly as the palette would."""
    cmd = next(c for c in app.get_system_commands(app.screen) if c.title == title)
    cmd.callback()


def guard_line():
    """The `budget_guard = ...` line of config.toml: CloudDeck edits that file (projects, timers) but must never touch this policy."""
    return next(ln for ln in ddc.CONFIG_FILE.read_text().splitlines() if ln.startswith("budget_guard"))


def notified(app, needle):
    return any(needle in n.message for n in app._notifications)


# ------------------------------------------------------------------ the app
def test_boots_and_all_tabs_open():
    async def t(app, pilot, p):
        for key in "12345":
            await goto(pilot, app, key)
        assert not app.problems
        await wait_for(lambda: "DEMO" in txt(app.query_one("#summary")) and "3 workers" in txt(app.query_one("#summary")), pilot, "the summary line")
    drive(t)


def test_deploy_dialog_shows_estimate_and_deploys_exactly_what_was_configured():
    async def t(app, pilot, p):
        await pilot.press("d")
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#cpu", Input).value = "4"
        s.query_one("#mem", Input).value = "8"
        s.query_one("#disk", Input).value = "20"
        await quote_for(pilot, s)
        assert "Expected" in txt(s.query_one("#quote")) and s.query_one("#cap", Input).value        # cap auto-filled from the estimate
        await ok(pilot, app)
        await confirm_open(pilot, app)                                 # review step before spending
        await ok(pilot, app)
        await wait_for(lambda: calls(p, "deploy"), pilot, "the deploy call")
        spec = calls(p, "deploy")[0]
        assert (spec.cpu, spec.mem_gb, spec.disk_gb, spec.role) == (4, 8, 20, "box") and spec.max_usd_hr > 0
        await wait_for(lambda: any(i.role == "box" for i in app.instances), pilot, "the new box in the instance list")
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to finish")
    drive(t)


def test_deploy_dialog_blocks_invalid_and_impossible_choices():
    async def t(app, pilot, p):
        await pilot.press("d")
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#cpu", Input).value = "0"
        await wait_for(lambda: "CPU" in txt(s.query_one("#err")), pilot, "the CPU error")
        s.query_one("#cpu", Input).value = "8"
        s.query_one("#role", Select).value = "dev"                       # a dev box is already running
        await wait_for(lambda: "already running" in txt(s.query_one("#err")), pilot, "the already-running error")
        await ok(pilot, app)
        await pilot.pause()
        assert app.screen is s and "already running" in txt(s.query_one("#err"))
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")
        assert not calls(p, "deploy") and nothing_started(app)
    drive(t)


def test_gpu_market_row_opens_deploy_with_that_gpu():
    async def t(app, pilot, p):
        await pilot.press("g")
        await wait_for(lambda: app.query_one("#gpu", FilterTable).table.row_count == 4 and app.focused is app.query_one("#gpu", FilterTable).table, pilot, "the GPU market, focused")
        await pilot.press("enter")
        s = await screen_is(pilot, app, DeployScreen)
        assert s.query_one("#gpu", Input).value == "1"
        await wait_for(lambda: s.query_one("#gpu_model", Select).value == "rtx4090", pilot, "the GPU model to be selected once the market list arrived")
    drive(t)


def test_terminate_worker_needs_confirmation_then_closes_only_that_worker():
    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        before = len(app.fleet)
        first = app.fleet[0].id
        await selected(pilot, app, "fleet", first)
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        assert s.word == "terminate"
        body = txt(s.body)                                                  # exactly what is destroyed, what it cost, what is saved
        assert app.fleet[0].name in body and "Spent so far" in body and "hold no data" in body
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")
        assert not calls(p, "terminate") and nothing_started(app)           # cancel does nothing
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        await wait_for(lambda: s.query_one("#ok").region.width > 0, pilot, "the dialog to be laid out")
        await pilot.click("#ok")                                           # the disabled button does nothing
        await pilot.pause()
        assert app.screen is s and nothing_started(app) and not calls(p, "terminate")
        await typed(pilot, app, "terminate")
        await wait_for(lambda: calls(p, "terminate") == [first], pilot, "the terminate call")
        await wait_for(lambda: len(app.fleet) == before - 1, pilot, "the worker to disappear")
    drive(t)


def test_terminate_dev_box_requires_typing_and_warns_about_workers():
    async def t(app, pilot, p):
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        assert "final checkpoint" in txt(s.body) and "3 worker" in txt(s.body)
        assert all(w.name in txt(s.body) for w in app.fleet) and "newest verified copy" in txt(s.body)
        assert s.query_one("#ok").disabled
        s.query_one("#word", Input).value = "nope"
        await pilot.pause()
        assert s.query_one("#ok").disabled
        s.query_one("#word", Input).value = "stop dev"
        await wait_for(lambda: not s.query_one("#ok").disabled, pilot, "the button to enable")
        await ok(pilot, app)
        await wait_for(lambda: app.dev is None and not app.fleet, pilot, "everything to close")
        assert len(calls(p, "terminate")) == 1
    drive(t)


def test_external_deployments_are_visible_but_cannot_be_terminated():
    async def t(app, pilot, p):
        tbl = app.query_one("#inst", FilterTable)
        await wait_for(lambda: tbl.table.row_count == len(app.instances), pilot, "all rows")
        tbl.table.move_cursor(row=tbl.table.row_count - 1)
        await wait_for(lambda: tbl.selected_key and app.instance(tbl.selected_key).managed is False, pilot, "the external row to be selected")
        await pilot.press("x")
        await wait_for(lambda: notified(app, "not created by CloudDeck"), pilot, "the refusal notice")
        assert not isinstance(app.screen, ConfirmScreen) and not calls(p, "terminate") and nothing_started(app)
        await pilot.press("z")                                            # resize is also refused
        await wait_for(lambda: notified(app, "Select an instance created by CloudDeck"), pilot, "the resize refusal notice")
        assert not isinstance(app.screen, DeployScreen)
    drive(t)


def test_resize_replaces_with_explicit_warning():
    async def t(app, pilot, p):
        worker = app.fleet[0].id
        await selected(pilot, app, "inst", worker)
        await pilot.press("z")
        s = await screen_is(pilot, app, DeployScreen)
        assert s.query_one("#role", Select).disabled
        s.query_one("#cpu", Input).value = "16"
        await quote_for(pilot, s)
        await ok(pilot, app)
        c = await confirm_open(pilot, app)
        assert "REPLACES" in txt(c.body)
        assert all(k in txt(c.body) for k in ("Destroys", "Replaced by", "Budget ceiling", "Spend guard", "Spent so far"))
        await typed(pilot, app, "resize")
        await wait_for(lambda: calls(p, "deploy") and calls(p, "terminate"), pilot, "the replacement to be deployed and the old one closed")
        assert calls(p, "deploy")[0].cpu == 16 and calls(p, "deploy")[0].role == "worker"
    drive(t)


def test_scale_workers_up_and_down():
    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        await pilot.press("s")
        form = await screen_is(pilot, app, FormScreen)
        form.query_one("#f-n", Input).value = "5"
        await ok(pilot, app)
        await confirm_open(pilot, app)
        assert "Budget ceiling" in txt(app.screen.body) and "Spend guard" in txt(app.screen.body)
        await ok(pilot, app)
        await wait_for(lambda: len(app.fleet) == 5, pilot, "five workers")
        assert calls(p, "scale") == [5]
        await close_task(pilot, app)
        await focus_in_pane(pilot, app)
        await pilot.press("s")
        form = await screen_is(pilot, app, FormScreen)
        form.query_one("#f-n", Input).value = "1"
        await ok(pilot, app)
        victims = [w.name for w in sorted(app.fleet, key=lambda w: w.started_at)[1:]]
        await confirm_open(pilot, app)
        assert all(v in txt(app.screen.body) for v in victims)             # it names the 4 workers it will close
        await typed(pilot, app, "scale down")
        await wait_for(lambda: len(app.fleet) == 1, pilot, "one worker")
    drive(t)


def test_dispatch_streams_merged_output_from_every_worker():
    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        await pilot.press("d")
        s = await screen_is(pilot, app, DispatchScreen)
        s.query_one("#cmd", Input).value = "echo hello"
        await wait_for(lambda: s.query_one("#run").region.width > 0, pilot, "the Run button to be laid out")
        await pilot.click("#run")
        await wait_for(lambda: "finished" in txt(s.query_one("#dstatus")), pilot, "the run to finish")
        assert "3 ok" in txt(s.query_one("#dstatus"))
        await wait_for(lambda: len(s.query_one("#out").lines) >= 9, pilot, "the output lines")       # a RichLog that was hidden defers its first writes until it has a size
        assert len(calls(p, "stream")) == 3
    drive(t)


def test_search_filters_provider_table():
    async def t(app, pilot, p):
        await pilot.press("p")
        tbl = app.query_one("#prov", FilterTable)
        await wait_for(lambda: tbl.table.row_count == 6 and app.focused is tbl.table, pilot, "the provider table, focused")
        await pilot.press("slash")
        await wait_for(lambda: isinstance(app.focused, Input), pilot, "the search box")
        await pilot.press(*"echo")
        await wait_for(lambda: tbl.table.row_count == 1, pilot, "the filtered table")
        await pilot.press("escape")
        await wait_for(lambda: tbl.table.row_count == 6 and app.focused is tbl.table, pilot, "the full table again")
        await pilot.press("f")                                            # GPU-only toggle
        await wait_for(lambda: tbl.table.row_count == 2, pilot, "the GPU-only table")
    drive(t)


def test_projects_add_forget_and_claude_sessions():
    async def t(app, pilot, p):
        policy = guard_line()
        await goto(pilot, app, "3")
        tbl = app.query_one("#proj", FilterTable)
        await wait_for(lambda: "main@3f9a1c2" in "".join(c.plain for c in tbl.table.get_row("web-app")), pilot, "the workspace state of web-app")
        await selected(pilot, app, "proj", "web-app")
        await pilot.press("n")                                            # new Claude session in web-app
        await wait_for(lambda: any(w[1] == "cc-web-app" for w in p.windows), pilot, "the Claude window")
        await quiet(pilot, app)
        await focus_in_pane(pilot, app)
        await pilot.press("a")
        s = await screen_is(pilot, app, FormScreen)
        s.query_one("#f-name", Input).value = "scratch"
        s.query_one("#f-path", Input).value = str(ddc.STATE_DIR)
        await ok(pilot, app)
        await wait_for(lambda: "scratch" in ddc.CONFIG_FILE.read_text(), pilot, "the project in config.toml")
        assert guard_line() == policy
        await wait_for(lambda: "scratch" in tbl._shown, pilot, "the project in the table")
        await selected(pilot, app, "proj", "scratch")
        await pilot.press("delete")
        c = await confirm_open(pilot, app)
        assert "NOT touched" in txt(c.body)
        await ok(pilot, app)
        await wait_for(lambda: "scratch" not in ddc.CONFIG_FILE.read_text(), pilot, "the project to be removed from config.toml")
    drive(t)


def test_recovery_checkpoint_snooze_settings_and_stop_everything():
    async def t(app, pilot, p):
        policy = guard_line()
        await goto(pilot, app, "5")
        await pilot.press("c")
        await wait_for(lambda: calls(p, "checkpoint") == [None], pilot, "the checkpoint call")
        await close_task(pilot, app)
        await wait_for(lambda: notified(app, "verified on this PC"), pilot, "the checkpoint toast (the task's `done` callback ran)")
        await focus_in_pane(pilot, app)
        await pilot.press("z")
        await screen_is(pilot, app, SelectScreen)
        await pilot.press("enter")
        await wait_for(lambda: calls(p, "hold_idle") == [30], pilot, "the snooze call")
        await quiet(pilot, app)
        await focus_in_pane(pilot, app)
        await pilot.press("t")
        s = await screen_is(pilot, app, FormScreen)
        s.query_one("#f-idle_minutes", Input).value = "90"
        await ok(pilot, app)
        await wait_for(lambda: ddc.CFG["idle_minutes"] == 90, pilot, "the setting to be saved")
        assert guard_line() == policy
        await quiet(pilot, app)
        await focus_in_pane(pilot, app)
        await pilot.press("X")
        c = await confirm_open(pilot, app)
        assert c.query_one("#ok").disabled
        assert all(i.name in txt(c.body) for i in app.instances if i.managed) and "never touched" in txt(c.body)
        await typed(pilot, app, "stop all")
        await wait_for(lambda: not any(i.managed for i in app.instances), pilot, "everything to close")
        assert any(not i.managed for i in app.instances)                  # the external deployment is untouched
    drive(t)


def test_narrow_and_short_terminals_stay_usable():
    async def t(app, pilot, p):
        await wait_for(lambda: app.screen.has_class("-narrow") and app.screen.has_class("-short"), pilot, "the narrow layout")
        for key in "12345":
            await goto(pilot, app, key)
        await goto(pilot, app, "1")
        await pilot.press("d")
        await screen_is(pilot, app, DeployScreen)                        # dialogs still fit
        await pilot.press("escape")
    drive(t, size=(80, 24))


def test_wide_terminal_gets_wide_layout():
    async def t(app, pilot, p):
        await wait_for(lambda: app.screen.has_class("-wide"), pilot, "the wide layout")
    drive(t, size=(180, 50))


def test_command_palette_lists_every_area():
    async def t(app, pilot, p):
        titles = {c.title for c in app.get_system_commands(app.screen)}
        assert {"Deploy instance…", "Scale workers to N…", "Checkpoint now", "Go to Monitor", "New Claude Code session…", "Snooze idle shutdown…",
                "Stop everything safely…", "Browse GPU market", "Add project…"} <= titles
    drive(t)


def test_errors_are_clear_and_do_not_crash():
    assert "price cap" in describe(ddc.DdcError("no acceptable bids"))
    assert "API key" in describe(ddc.DdcError("HTTP 401 GET /v1/balances: x"))
    assert describe(ValueError("x")).startswith("ValueError: x")

    class Failing(type(providers.get("demo"))):
        def deploy(self, spec, log=print):
            raise providers.ProviderError("no acceptable bids")

    async def t(app, pilot, p):
        await pilot.press("d")
        s = await screen_is(pilot, app, DeployScreen)
        await quote_for(pilot, s)
        await ok(pilot, app)
        await confirm_open(pilot, app)
        await ok(pilot, app)
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to fail")
        await wait_for(lambda: "price cap" in txt(app.screen.query_one("#task-status")), pilot, "the readable error")
        await wait_for(lambda: notified(app, "price cap"), pilot, "the error toast")
        assert app.is_running
    drive(t, provider=Failing())


def test_quit_asks_first_and_theme_is_remembered():
    async def t(app, pilot, p):
        await pilot.press("q")
        c = await confirm_open(pilot, app)
        assert "Leases keep running" in txt(c.body)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not c, pilot, "the dialog to close")
        assert app.is_running
        app.theme = "nord"
        await wait_for(lambda: ddc.read_json(ddc.STATE_DIR / "clouddeck.json", {}).get("theme") == "nord", pilot, "the theme to be saved")
    drive(t)


def test_ddc_log_lines_reach_the_events_log():
    async def t(app, pilot, p):
        ddc.log("hello from the engine")
        await wait_for(lambda: any("hello from the engine" in e for e in app.events), pilot, "the line in the events log")
        assert "hello from the engine" in ddc.LOG.read_text()
    drive(t)


def test_the_controller_stops_logging_into_a_closed_ui():
    """A closed app must put the controller's log back: otherwise every later log line goes through a dead UI (and, in one case, broke the controller)."""
    before = ddc.log
    drive(lambda app, pilot, p: asyncio.sleep(0))
    assert ddc.log is before
    ddc.log("still works after the UI is gone")


# ------------------------------------------------------------------ destructive actions: every entry point is typed and detailed
# (how it is reached, tab to be on, the phrase it asks for, what the provider must have been asked to do once the phrase was typed)
async def _compute_x(pilot, app):
    await selected(pilot, app, "inst", app.dev.id)
    await pilot.press("x")


async def _compute_palette_terminate(pilot, app):
    await selected(pilot, app, "inst", app.dev.id)
    palette_run(app, "Terminate selected instance…")


async def _compute_resize(pilot, app):
    await selected(pilot, app, "inst", app.dev.id)
    await pilot.press("z")
    s = await screen_is(pilot, app, DeployScreen)
    s.query_one("#cpu", Input).value = "16"
    await quote_for(pilot, s)
    await ok(pilot, app)


async def _workers_x(pilot, app):
    await goto(pilot, app, "2")
    await selected(pilot, app, "fleet", app.fleet[0].id)
    await pilot.press("x")


async def _workers_scale_to_zero(pilot, app):
    await goto(pilot, app, "2")
    await pilot.press("s")
    s = await screen_is(pilot, app, FormScreen)
    s.query_one("#f-n", Input).value = "0"
    await ok(pilot, app)


async def _node_inspector_x(pilot, app):
    await goto(pilot, app, "2")
    await selected(pilot, app, "fleet", app.fleet[0].id)
    await pilot.press("enter")
    await wait_for(lambda: type(app.screen).__name__ == "NodeScreen", pilot, "the node inspector")
    await pilot.press("x")


async def _recovery_X(pilot, app):
    await goto(pilot, app, "5")
    await pilot.press("X")


async def _recovery_button(pilot, app):
    await goto(pilot, app, "5")
    await wait_for(lambda: not app.query_one("#b-stop").disabled and app.query_one("#b-stop").region.width > 0, pilot, "the Stop everything button")
    await pilot.click("#b-stop")


async def _recovery_palette(pilot, app):
    await goto(pilot, app, "5")
    palette_run(app, "Stop everything safely…")


DESTRUCTIVE = {   # id: (trigger, phrase, expected provider call)
    "compute x on the dev box": (_compute_x, "stop dev", "terminate"),
    "compute palette terminate": (_compute_palette_terminate, "stop dev", "terminate"),
    "compute resize the dev box": (_compute_resize, "resize", "terminate"),
    "workers x": (_workers_x, "terminate", "terminate"),
    "workers scale to 0": (_workers_scale_to_zero, "scale down", "terminate"),
    "node inspector x": (_node_inspector_x, "terminate", "terminate"),
    "recovery X": (_recovery_X, "stop all", "terminate"),
    "recovery Stop everything button": (_recovery_button, "stop all", "terminate"),
    "recovery palette stop all": (_recovery_palette, "stop all", "terminate"),
}


@pytest.mark.parametrize("trigger,phrase,call", list(DESTRUCTIVE.values()), ids=list(DESTRUCTIVE))
def test_every_destructive_entry_point_shows_details_and_needs_a_typed_phrase(trigger, phrase, call):
    """Keys, buttons, the node inspector and command-palette entries all end in a ConfirmScreen with a phrase and the details; nothing reaches the
    provider before the phrase is typed exactly, and cancelling leaves everything as it was."""
    async def t(app, pilot, p):
        await trigger(pilot, app)
        s = await confirm_open(pilot, app)
        body = txt(s.body)
        assert s.word == phrase and s.danger and s.query_one("#ok").disabled
        assert "Destroys" in body and "Checkpoint" in body and "Spent so far" in body
        assert not p.calls or all(c[0] in ("screen",) for c in p.calls)           # nothing was sent to the provider by merely opening it
        for wrong in (phrase[:-1], phrase.upper(), "yes", " "):
            s.query_one("#word", Input).value = wrong
            await pilot.pause()
            assert s.query_one("#ok").disabled, wrong
        await pilot.press("escape")                                                # cancel: the same dialog, cancelled, starts nothing
        await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")
        assert nothing_started(app) and not calls(p, call)
        await trigger(pilot, app)
        await typed(pilot, app, phrase)                                            # now for real
        await wait_for(lambda: calls(p, call), pilot, f"the provider to be asked ({call})")
    drive(t)


def test_nothing_destructive_reaches_the_provider_from_any_key_or_palette_entry_without_a_typed_phrase():
    """Press every key bound in the app and every pane, run every command-palette entry of the app, cancel whatever opens: the provider must
    never have been asked to close, resize or scale anything.  New keys and entries are covered automatically."""
    from clouddeck.tui import compute, monitor, projects, recovery, workers

    async def t(app, pilot, p):
        app.interactive = lambda fn, what: None                                   # attaching / shells need a real terminal: not what is tested here

        async def back():
            await quiet(pilot, app)
            for _ in range(8):
                if app.screen is app.screen_stack[0] and type(app.focused).__name__ != "SelectOverlay":    # an open drop-down list (log source) is not a screen
                    return
                await pilot.press("escape")
                await quiet(pilot, app)
            raise AssertionError(f"could not get back to the main screen from {type(app.screen).__name__}")

        keys = []
        for tab, pane in (("1", compute.ComputePane), ("2", workers.WorkersPane), ("3", projects.ProjectsPane), ("4", monitor.MonitorPane), ("5", recovery.RecoveryPane)):
            for b in pane.BINDINGS:
                keys += [(tab, k) for k in b.key.split(",")]
        for tab, key in keys:
            await goto(pilot, app, tab)
            await pilot.press(key)
            await back()
        builtin = {c.title for c in App.get_system_commands(app, app.screen)}      # Textual's own entries (quit, theme, screenshot ...) are not ours to test
        ours = [c.title for c in app.get_system_commands(app.screen) if c.title not in builtin]
        assert len(ours) >= 15, ours
        for title in ours:
            cmd = next(c for c in app.get_system_commands(app.screen) if c.title == title)
            res = cmd.callback()
            if asyncio.iscoroutine(res):
                await res
            await back()
        harmless = {"screen", "checkpoint", "verify", "heal", "hold_idle", "stream"}
        bad = [c for c in p.calls if c[0] not in harmless]
        assert not bad, bad
    drive(t)


def test_every_close_in_the_tui_is_inside_a_typed_confirmation():
    """Static guard: a provider call that closes things (terminate / resize / stop_all) may only appear inside the `on_ok` of `confirm_destroy`
    (or in the one retry that `discard_prompt` makes after the user typed the words).  A new call site elsewhere fails this test."""
    destructive = {"terminate", "resize", "stop_all"}
    root = Path(__file__).resolve().parent.parent / "clouddeck" / "tui"
    found, covered = [], set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        inside = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "confirm_destroy":
                for kw in node.keywords:
                    if kw.arg == "on_ok":
                        inside |= {id(n) for n in ast.walk(kw.value)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in destructive and "provider" in ast.unparse(node.func.value):
                found.append(f"{path.name}:{node.lineno} {ast.unparse(node.func)}")
                if id(node) in inside:
                    covered.add(f"{path.name}:{node.lineno} {ast.unparse(node.func)}")
    assert found, "the scan found no destructive call at all: the test is broken"
    assert not set(found) - covered, f"destructive provider calls outside confirm_destroy(on_ok=...): {sorted(set(found) - covered)}"


def test_terminate_dialog_says_when_checkpoints_are_off_or_missing():
    class NoCheckpoints(type(providers.get("demo"))):
        def checkpoint_enabled(self):
            return False

    async def t(app, pilot, p):
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        assert "CHECKPOINTS ARE OFF" in txt(s.body)
    drive(t, provider=NoCheckpoints())

    class NoCopy(type(providers.get("demo"))):
        def checkpoints(self):
            return []

    async def t2(app, pilot, p):
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        assert "none yet" in txt(s.body)
    drive(t2, provider=NoCopy())


def test_deploy_shows_the_guard_verdict_and_a_refusal_blocks_it():
    async def t(app, pilot, p):
        await pilot.press("d")
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#cpu", Input).value = "4"
        await quote_for(pilot, s)
        assert "Budget ceiling" in txt(s.query_one("#quote")) and "Spend guard" in txt(s.query_one("#quote")) and s.verdict.ok
        await ok(pilot, app)
        c = await confirm_open(pilot, app)
        assert "Budget ceiling" in txt(c.body)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not c, pilot, "the dialog to close")
        p.limits["budget_usd"] = 1.0                                              # now far below the worst case
        await pilot.press("d")
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#cpu", Input).value = "5"
        await wait_for(lambda: s.verdict is not None and not s.verdict.ok and "5 vCPU" in txt(s.query_one("#quote")), pilot, "the refusing verdict")
        await ok(pilot, app)
        await wait_for(lambda: "Refused by the spend guard" in txt(s.query_one("#err")), pilot, "the refusal in the dialog")
        assert app.screen is s
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")
        assert not calls(p, "deploy") and nothing_started(app)
    drive(t)


def test_resize_and_scale_up_are_refused_by_the_guard_before_any_confirmation():
    async def t(app, pilot, p):
        p.limits["budget_guard"] = False                                          # the fixture has the guard on, and the demo provider has no balance; count/price limits hold anyway
        p.limits["max_workers"] = 3                                               # already 3 workers
        await goto(pilot, app, "2")
        await pilot.press("plus")
        await quiet(pilot, app)                                                   # the guard answered (a refusal opens nothing)
        assert not isinstance(app.screen, ConfirmScreen) and not calls(p, "deploy")
        assert any("max_workers" in e for e in app.events)
        await pilot.press("s")
        form = await screen_is(pilot, app, FormScreen)
        form.query_one("#f-n", Input).value = "4"
        await ok(pilot, app)
        await wait_for(lambda: app.screen is not form, pilot, "the form to close")
        await quiet(pilot, app)
        assert not isinstance(app.screen, ConfirmScreen) and not calls(p, "scale")
        p.limits["max_workers"] = 6
        p.limits["max_usd_hr_hard"] = 0.01                                        # resize to anything costing more is refused, the old box stays
        await goto(pilot, app, "1")
        await selected(pilot, app, "inst", app.fleet[0].id)
        await pilot.press("z")
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#cpu", Input).value = "16"
        await quote_for(pilot, s)
        await ok(pilot, app)
        await wait_for(lambda: app.screen is not s or "Refused by the spend guard" in txt(s.query_one("#err")), pilot, "the refusal")
        await quiet(pilot, app)
        assert not isinstance(app.screen, ConfirmScreen) and not calls(p, "terminate")
    drive(t)


def test_a_guard_that_cannot_be_evaluated_is_a_refusal_not_a_pass():
    class Broken(type(providers.get("demo"))):
        def guard(self, spec, n=1, replacing=None):
            raise providers.ProviderError("state file is damaged")

    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        await pilot.press("plus")
        await quiet(pilot, app)
        assert not isinstance(app.screen, ConfirmScreen) and not calls(p, "deploy")
        assert any("could not check the spend guard" in e and "damaged" in e for e in app.events)
    drive(t, provider=Broken())


def test_with_checkpoints_off_closing_the_dev_box_needs_the_words_discard_workspace():
    """The controller refuses to close a dev box it cannot checkpoint unless told; the dialog says so in red and the phrase says what is lost."""
    async def t(app, pilot, p):
        p.checkpoints_on = False
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")                                                    # dev box
        s = await confirm_open(pilot, app)
        assert s.word == "discard workspace" and "CHECKPOINTS ARE OFF" in txt(s.body)
        await typed(pilot, app, "discard workspace")
        await wait_for(lambda: app.dev is None, pilot, "the dev box to close")
        assert calls(p, "discard") == [calls(p, "terminate")[0]]
    drive(t)


def test_demo_refuses_to_close_an_unsaved_dev_box_without_the_discard_flag():
    p = providers.get("demo")
    p.checkpoints_on = False
    dev = next(i for i in p.instances() if i.role == "dev")
    with pytest.raises(providers.ProviderError, match="DESTROY"):
        p.terminate(dev.id)
    assert dev.id in {i.id for i in p.instances()}
    p.terminate(dev.id, discard_workspace=True)
    assert not [i for i in p.instances() if i.role in ("dev", "worker")]


def test_stop_everything_with_checkpoints_off_passes_the_discard_flag_after_the_typed_phrase():
    async def t(app, pilot, p):
        p.checkpoints_on = False
        await goto(pilot, app, "5")
        await pilot.press("X")
        await typed(pilot, app, "discard workspace")
        await wait_for(lambda: not any(i.managed for i in app.instances), pilot, "everything to close")
        assert calls(p, "discard")
    drive(t)


# ------------------------------------------------------------------ the controller still says no after the dialog: the user is asked again, by typing
def test_a_refusal_after_the_dialog_asks_for_the_words_discard_workspace_and_cancel_keeps_everything():
    """The dialog was shown while checkpoints were on; state_repo was emptied before the close ran, so the controller refuses (DiscardRefused).
    Nothing is closed; the user gets a second dialog that shows the checkpoint status and needs `discard workspace` typed; Esc keeps it all."""
    async def t(app, pilot, p):
        p.limits["budget_guard"] = False                                          # the fixture has the guard on; the demo provider has no balance to judge it with
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        assert s.word == "stop dev"                                               # checkpoints were on when the dialog opened
        p.checkpoints_on = False                                                  # ... and are off by the time the provider runs
        s.query_one("#word", Input).value = "stop dev"
        await ok(pilot, app)
        await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.word == "discard workspace", pilot, "the second, typed dialog")
        second = app.screen
        assert "CHECKPOINTS ARE OFF" in txt(second.body) and "Nothing closed" in txt(second.body) and "still running" in txt(second.body)
        assert second.query_one("#ok").disabled and not calls(p, "discard") and app.dev is not None
        await pilot.press("escape")                                               # cancel
        await wait_for(lambda: app.screen is not second, pilot, "the dialog to close")
        await close_task(pilot, app)
        assert app.dev is not None and not calls(p, "discard") and app.retry_discard is None
    drive(t)


def test_a_refusal_after_the_dialog_can_be_overruled_only_by_typing_discard_workspace():
    async def t(app, pilot, p):
        p.limits["budget_guard"] = False                                          # the fixture has the guard on; the demo provider has no balance to judge it with
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")
        s = await confirm_open(pilot, app)
        p.checkpoints_on = False
        s.query_one("#word", Input).value = "stop dev"
        await ok(pilot, app)
        await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.word == "discard workspace", pilot, "the second, typed dialog")
        second = app.screen
        second.query_one("#word", Input).value = "stop dev"                        # the first phrase is not enough
        await pilot.pause()
        assert second.query_one("#ok").disabled
        await typed(pilot, app, "discard workspace")
        await wait_for(lambda: app.dev is None, pilot, "the dev box to close")
        assert calls(p, "discard") and app.retry_discard is None
    drive(t)


def test_a_later_unrelated_failure_never_replays_an_old_destructive_action():
    """`retry_discard` is cleared when the task ends: a checkpoint that fails with the same words must not reopen the old close."""
    class Flaky(type(providers.get("demo"))):
        def checkpoint(self, log=print):
            raise providers.ProviderError("checkpoints are disabled (state_repo is empty)")

    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        await selected(pilot, app, "fleet", app.fleet[0].id)
        await pilot.press("x")
        await typed(pilot, app, "terminate")
        await close_task(pilot, app)
        assert app.retry_discard is None
        await goto(pilot, app, "5")
        await pilot.press("c")                                                    # fails with "checkpoints are disabled"
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the checkpoint to fail")
        await quiet(pilot, app)
        assert isinstance(app.screen, TaskScreen) and len(calls(p, "terminate")) == 1          # no new dialog on top, no second terminate
    drive(t, provider=Flaky())


def test_the_real_akash_provider_refuses_in_a_way_the_tui_recognises_and_closes_only_after_discard(fake):
    """The TUI recognises the controller's DiscardRefused through the provider's re-wrapped error (terminate and stop everything), against the
    in-memory Akash fake: nothing is closed until discard_workspace is passed."""
    import types

    from clouddeck.providers.akash import AkashProvider
    from clouddeck.tui.safety import is_discard_refusal
    ddc.cmd_start(types.SimpleNamespace(profile="32", workers=0, worker_profile=None))
    ddc.CFG["state_repo"] = ""
    p = AkashProvider()
    dev = next(i for i in p.instances() if i.role == "dev")
    assert not p.checkpoint_enabled()
    with pytest.raises(providers.ProviderError) as e:
        p.terminate(dev.id)
    assert is_discard_refusal(e.value) and isinstance(e.value.__cause__, ddc.DiscardRefused) and fake.open_dseqs()
    with pytest.raises(providers.ProviderError) as e2:
        p.stop_all()
    assert is_discard_refusal(e2.value) and fake.open_dseqs()
    assert not is_discard_refusal(providers.ProviderError("no acceptable bids"))
    p.terminate(dev.id, discard_workspace=True)
    assert not fake.open_dseqs()


# ------------------------------------------------------------------ Stop everything lists exactly what the provider will close
def test_stop_everything_names_what_stays_running_instead_of_listing_it_as_destroyed():
    class Keeps(type(providers.get("demo"))):
        stop_all_roles = ("dev", "worker")                                        # like `ddc stop`: hand-made "box" leases stay until named

        def stop_all(self, log=print, *, discard_workspace=False):
            self.calls.append(("stop_all", discard_workspace))
            for i in [i for i in self.instances() if i.managed and i.role in self.stop_all_roles]:
                if i.id in self._items:
                    self.terminate(i.id, log, discard_workspace=discard_workspace)

    p = Keeps()
    box = p.deploy(Spec(cpu=4, mem_gb=8, disk_gb=20, role="box", max_usd_hr=0.5, hours=4))

    async def t(app, pilot, p):
        await goto(pilot, app, "5")
        await pilot.press("X")
        s = await confirm_open(pilot, app)
        body = txt(s.body)
        destroys = body.split("Spent so far")[0]
        assert all(i.name in destroys for i in app.instances if i.managed and i.role in ("dev", "worker")) and box.name not in destroys
        assert "STAYS RUNNING" in body and box.name in body.split("STAYS RUNNING")[1]
        await typed(pilot, app, "stop all")
        await wait_for(lambda: not app.dev and not app.fleet, pilot, "the dev box and workers to close")
        await quiet(pilot, app)
        assert box.id in {i.id for i in app.instances}                           # what the dialog said would stay did stay
    drive(t, provider=p)


def test_stop_everything_with_only_unlisted_leases_explains_instead_of_opening_an_empty_dialog():
    class OnlyBoxes(type(providers.get("demo"))):
        stop_all_roles = ("dev", "worker")

    p = OnlyBoxes(populate=False)
    p.limits["budget_guard"] = False                                              # the fixture has the guard on; the demo provider has no balance to judge it with
    p.deploy(Spec(cpu=4, mem_gb=8, disk_gb=20, role="box", max_usd_hr=0.5, hours=4))

    async def t(app, pilot, p):
        await goto(pilot, app, "5")
        await pilot.press("X")
        await wait_for(lambda: notified(app, "not closed by Stop everything"), pilot, "the explanation")
        assert not isinstance(app.screen, ConfirmScreen)
    drive(t, provider=p)


# ------------------------------------------------------------------ misc
def test_a_damaged_state_file_is_shown_as_a_problem_not_a_crash():
    from clouddeck.engine import DdcError

    class Damaged(type(providers.get("demo"))):
        broken = False

        def instances(self, refresh=False):
            if self.broken:
                raise ddc.StateCorrupt("state.json is unusable. ddc will NOT create leases until this is resolved.")
            return super().instances(refresh)

    async def t(app, pilot, p):
        p.broken = True
        app.poll_network()
        await wait_for(lambda: "network" in app.problems, pilot, "the problem to be reported")
        assert "will NOT create leases" in app.problems["network"] and "ddc reconcile" in app.problems["network"]    # message plus the hint
        assert app.is_running and "problem" in txt(app.query_one("#summary"))
        assert issubclass(ddc.StateCorrupt, DdcError)
        p.broken = False
        app.poll_network()
        await wait_for(lambda: "network" not in app.problems, pilot, "the problem to clear")
    drive(t, provider=Damaged())


def test_node_inspector_opens_shows_the_probe_and_closes():
    async def t(app, pilot, p):
        await goto(pilot, app, "2")
        await selected(pilot, app, "fleet", app.fleet[0].id)
        await pilot.press("enter")
        await wait_for(lambda: type(app.screen).__name__ == "NodeScreen" and app.screen.probe is not None, pilot, "the inspector with its probe")
        assert "CPU" in txt(app.screen.query_one("#node-info")) and "Job" in txt(app.screen.query_one("#node-info"))
        await pilot.press("escape")
        await wait_for(lambda: type(app.screen).__name__ != "NodeScreen", pilot, "the inspector to close")
        assert app.is_running
    drive(t)


def test_the_probe_timer_firing_during_teardown_is_harmless():
    """poll_probes used to raise NoMatches when its timer fired after the tabs were gone (app shutdown)."""
    from textual.css.query import NoMatches

    async def t(app, pilot, p):
        def gone(*a, **k):
            raise NoMatches("#tabs")
        started = []
        app.poll = lambda *a, **k: started.append(a)
        app.query_one = gone
        app.poll_probes()
        assert not started, "nothing is probed while the UI is going away"
    drive(t)


def test_the_help_text_lists_exactly_the_keys_each_pane_binds():
    import re

    from clouddeck.tui.app import HELP
    from clouddeck.tui.compute import ComputePane
    from clouddeck.tui.monitor import MonitorPane
    from clouddeck.tui.projects import ProjectsPane
    from clouddeck.tui.recovery import RecoveryPane
    from clouddeck.tui.workers import WorkersPane
    names = {"plus,equals_sign": "+", "delete": "Delete"}
    for title, pane in (("Compute", ComputePane), ("Workers", WorkersPane), ("Projects", ProjectsPane), ("Monitor", MonitorPane), ("Recovery", RecoveryPane)):
        line = next(ln for ln in HELP.splitlines() if ln.startswith(f"[b]{title}[/b]"))
        listed = {item.strip().split(" ")[0] for item in line.split("[/b]", 1)[1].split("·")} - {"Enter"}
        listed = {k for item in listed for k in (item.split("/") if re.fullmatch(r"[a-z](/[a-z])+", item) else [item])}      # "i/p/g" = three keys
        bound = {names.get(b.key, b.key) for b in pane.BINDINGS} - {"tab"}                                                 # Tab (switch table) is a focus key, not an action
        assert listed == bound, f"{title}: help lists {sorted(listed)} but the pane binds {sorted(bound)}"
