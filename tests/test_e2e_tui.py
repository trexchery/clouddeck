"""End-to-end CloudDeck: the service / provider layer on top of the REAL controller (AkashProvider over the fake Akash), and the real TUI driven by key
presses and clicks against the simulated provider and against the Akash provider on the fake.  Nothing real is touched.  Windows and Linux."""
import asyncio
import threading
import time

import pytest
from textual.widgets import DataTable, Input, Select

from clouddeck import providers
from clouddeck.engine import ddc
from clouddeck.providers.base import ProviderError, Spec
from clouddeck.providers.demo import DemoProvider
from clouddeck.service import Service
from clouddeck.tui.app import CloudDeckApp
from clouddeck.tui.compute import DeployScreen
from clouddeck.tui.widgets import ConfirmScreen, FormScreen, TaskScreen

pytest.register_assert_rewrite("e2e_helpers")      # so assertion rewriting also applies to the helpers (they are loaded as a plugin below)
from e2e_helpers import akash_on, assert_foreign_untouched, created, deleted, deps, ledger_events, live, text_of, watchdog  # noqa: E402

pytest_plugins = ["e2e_helpers"]       # the e2e / fleet fixtures
DEV = Spec(cpu=32, mem_gb=64, disk_gb=100, role="dev", max_usd_hr=0.9, hours=8, preset="32")
WORKER = DEV.with_(role="worker", hours=4)


def names(p, role):
    return [i for i in p.instances() if i.role == role and i.managed]


# ------------------------------------------------------------------------------------------------ provider layer (what every screen calls)
def test_provider_lifecycle_deploy_scale_terminate_with_the_checkpoint_before_the_close(e2e):
    foreign = e2e.add_foreign()
    p = akash_on(e2e)
    assert p.guard(DEV).ok and p.checkpoint_enabled()
    dev = p.deploy(DEV)
    assert dev.role == "dev" and dev.state == "running" and dev.host and ddc.dev_box()["dseq"] == dev.id
    assert [i.role for i in p.instances(True)] == ["dev", "external"], "the foreign deployment is visible, never managed"
    assert [i.managed for i in p.instances() if i.role == "external"] == [False]
    ws = p.scale(2, WORKER)
    assert [w.meta["idx"] for w in ws] == [1, 2] and len(e2e.open_dseqs()) == 4
    assert [v.meta["idx"] for v in p.scale_victims(1)] == [2]
    ws = p.scale(1, WORKER)
    assert [w.meta["idx"] for w in ws] == [1] and len(deleted(e2e)) == 1
    w3 = p.deploy(WORKER)                                              # a single worker through deploy(): the free number is reused
    assert w3.meta["idx"] == 2

    with pytest.raises(ProviderError, match="not created by ddc"):
        p.terminate(foreign)                                           # never, whatever the UI passes
    assert_foreign_untouched(e2e, foreign)
    cost = p.terminate(w3.id)
    assert cost >= 0 and len(live("worker")) == 1
    n = len(e2e.checkpoint_runs)
    p.terminate(dev.id)                                                # final checkpoint verified first, workers then dev box closed
    assert e2e.checkpoint_runs[n][0] == "final-stop" and live() == [] and e2e.open_dseqs() == [foreign]
    assert len(ledger_events("closed")) == 4


def test_terminating_the_dev_box_is_refused_untouched_when_the_checkpoint_fails_or_checkpoints_are_off(fleet):
    p = akash_on(fleet)
    dev = ddc.dev_box()["dseq"]
    fleet.checkpoint = "fail"
    with pytest.raises(ProviderError, match="cannot reach the state repo"):
        p.terminate(dev)
    assert len(fleet.open_dseqs()) == 3 and not deleted(fleet), "workers are not closed behind a refused dev box"
    fleet.checkpoint = "ok"
    ddc.CFG["state_repo"] = ""
    assert not p.checkpoint_enabled()
    with pytest.raises(ProviderError, match="DESTROY"):
        p.terminate(dev)
    assert len(fleet.open_dseqs()) == 3
    p.terminate(dev, discard_workspace=True)                           # only the typed "discard workspace" gets here
    assert fleet.open_dseqs() == [] and not fleet.commands("checkpoint.sh final-stop")[1:]


def test_the_guard_refuses_before_anything_is_created_when_the_money_is_not_there_or_not_known(e2e):
    p = akash_on(e2e)
    e2e.balance = 16.0
    v = p.guard(DEV)
    assert not v.ok and ("budget" in v.text.lower() or "reserve" in v.text.lower()), v.text
    with pytest.raises(ProviderError):
        p.deploy(DEV)
    assert created(e2e) == 0
    e2e.balance = 100.0
    p._cache.clear()
    e2e.offline = True
    v = p.guard(DEV)
    assert not v.ok and "balance unknown" in v.text, "fail closed: an unreadable balance is a refusal"
    with pytest.raises(ProviderError):
        p.deploy(DEV)
    assert created(e2e) == 0 and e2e.deployments == {}


def test_offline_the_tool_shows_what_it_knows_changes_nothing_and_catches_up_when_the_network_is_back(fleet):
    p = akash_on(fleet)
    ids = sorted(d["dseq"] for d in live())
    worker = ddc.workers()[0]["dseq"]
    fleet.offline = True
    p._cache.clear()
    assert sorted(i.id for i in p.instances(True) if i.managed) == ids, "the last known state, from the local records: no exception"
    assert p.spend().balance is None and "connection failed" in p.last_error
    with pytest.raises(ProviderError):
        p.terminate(worker)                                             # cannot be confirmed: the error is shown ...
    assert sorted(d["dseq"] for d in live()) == ids and all(d["status"] != "closed" for d in live()), "... and an unconfirmed close is never recorded as closed"
    v = p.guard(WORKER)
    assert not v.ok and "balance unknown" in v.text
    fleet.deployments[worker]["state"] = "closed"                       # meanwhile the platform closed it
    fleet.offline = False
    p._cache.clear()
    assert worker not in [i.id for i in p.instances(True)] and deps()[worker]["status"] == "closed", "caught up from the API once it answers"


def test_service_probes_every_node_and_reports_the_unreachable_one_without_breaking_the_rest(fleet):
    p = akash_on(fleet)
    svc = Service(p)
    insts = p.instances()
    res = svc.probe_all(insts)
    assert len(res) == 3 and all(r.ok and r.cpus == 32 for r in res.values())
    bad = next(i for i in insts if i.role == "worker")
    fleet.unreachable.add(bad.host)
    res = svc.probe_all(insts)
    same_host = {i.id for i in insts if i.host == bad.host}
    assert not res[bad.id].ok and "Connection timed out" in res[bad.id].error
    assert all(r.ok for k, r in res.items() if k not in same_host), "the others are unaffected"


def test_a_job_dispatched_from_clouddeck_keeps_the_workers_alive_through_the_heartbeat(fleet):
    p = akash_on(fleet)
    svc = Service(p)
    fleet.busy = "claude"
    fleet.minutes(8)
    ws = [i for i in p.instances() if i.role == "worker"]
    lines = []
    before = set(threading.enumerate())
    results = svc.dispatch("echo hello", ws, lambda host, line: lines.append((host, line)))
    assert [r.code for r in results] == [0, 0]
    for t in set(threading.enumerate()) - before:                       # the heartbeat is a daemon thread: wait for its last beat (at the end of the job)
        t.join(10)
    assert fleet.worker_activity == fleet.clock
    assert watchdog(fleet, 8) == 0 and len(ddc.workers()) == 2, "8 minutes after the job: still inside the grace period"
    assert watchdog(fleet, 4) == 0 and ddc.workers() == [], "12 minutes after it: idle, closed"


def test_snooze_from_clouddeck_holds_an_idle_stop_but_never_the_runtime_limit(fleet, monkeypatch):
    import types

    import clouddeck.providers.akash as akash_mod
    monkeypatch.setattr(akash_mod, "time", types.SimpleNamespace(time=lambda: fleet.clock, sleep=lambda s: None))     # the provider's wall clock is the fake one
    p = akash_on(fleet)
    until = p.hold_idle(60)
    assert until == fleet.clock + 3600 and p.idle_state().held_until == until
    fleet.minutes(50)                                                   # idle past the 45-minute limit
    fleet.worker_activity = fleet.clock
    assert watchdog(fleet, 1) == 0 and len(fleet.open_dseqs()) == 3, "snoozed"
    fleet.busy = "claude"
    fleet.minutes(8 * 60)
    assert watchdog(fleet, 0) == 0 and fleet.open_dseqs() == [], "the runtime limit is not snoozable"


def test_provider_checkpoint_verify_and_heal_go_through_the_controller(e2e):
    ddc.CFG["max_leases_per_provider"] = 1                              # every worker on its own host: a restart hits one of them
    p = akash_on(e2e)
    p.deploy(DEV)
    p.scale(1, WORKER)
    p.scale(2, WORKER)                                                  # one at a time: parallel workers may pick the same provider
    info = p.checkpoint()
    assert info.reason == "final" and e2e.commands("checkpoint.sh manual")
    assert p.verify().created_at
    w = ddc.workers()[1]
    e2e.restarted.add(w["host"])
    assert p.heal() == ["w02"] and not e2e.restarted and not deleted(e2e)


# ------------------------------------------------------------------------------------------------ the TUI
TABS = dict(zip("12345", ["compute", "workers", "projects", "monitor", "recovery"]))
WAIT = 30.0


async def wait_for(pred, pilot, what="the expected state", timeout=WAIT):
    end = time.monotonic() + timeout
    err = None
    while True:
        try:
            if pred():
                return
            err = None
        except Exception as e:    # a widget that is not mounted yet is "not ready"
            err = e
        if time.monotonic() > end:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what} (screen: {type(pilot.app.screen).__name__}; last error: {err!r})")
        await pilot.pause(0.02)


async def screen_is(pilot, app, cls):
    """The screen is on the stack before it has composed its widgets: wait until it is mounted, or a query for its children races the compose."""
    await wait_for(lambda: isinstance(app.screen, cls) and app.screen.is_mounted, pilot, f"the {cls.__name__} to open")
    return app.screen


async def goto(pilot, app, key):
    tab = TABS[key]
    await pilot.press(key)
    await wait_for(lambda: app.query_one("#tabs").active == tab and app.focused is not None and tab in {getattr(a, "id", None) for a in app.focused.ancestors},
                   pilot, f"the {tab} tab with the focus inside it")


async def ok(pilot, app):
    await wait_for(lambda: (b := app.screen.query_one("#ok")).region.width > 0 and not b.disabled, pilot, "the dialog's OK button")
    await pilot.click("#ok")


async def confirm_open(pilot, app):
    await wait_for(lambda: isinstance(app.screen, ConfirmScreen) and app.screen.query("#ok"), pilot, "a confirmation dialog")
    return app.screen


async def typed(pilot, app, word):
    s = await confirm_open(pilot, app)
    assert s.word == word and s.query_one("#ok").disabled, "a destructive step starts disabled and asks for its phrase"
    s.query_one("#word", Input).value = word
    await ok(pilot, app)
    await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")


async def close_task(pilot, app):
    await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to finish")
    await pilot.press("escape")
    await wait_for(lambda: not isinstance(app.screen, TaskScreen), pilot, "the task window to close")


async def selected(pilot, app, table, key):
    tbl = app.query_one(f"#{table}")
    await wait_for(lambda: key in tbl._shown, pilot, f"row {key} in {table}")
    tbl.table.move_cursor(row=tbl._shown.index(key))
    await wait_for(lambda: tbl.selected_key == key, pilot, f"row {key} selected")


def drive(fn, provider, ready=lambda app: len(app.instances) >= 1):
    async def go():
        app = CloudDeckApp(provider, poll_s=0.5, net_s=1.0, probe_s=1.0, money_s=1.0)
        async with app.run_test(size=(150, 45)) as pilot:
            await wait_for(lambda: ready(app) and app.spend is not None and isinstance(app.focused, DataTable), pilot, "the first data and the focus")
            await pilot.pause()
            await fn(app, pilot)
    asyncio.run(go())


def test_ui_lifecycle_on_the_simulated_provider_deploy_scale_and_typed_terminations():
    p = providers.get("demo").__class__(populate=False)

    async def t(app, pilot):
        await pilot.press("d")                                        # deploy the dev box
        s = await screen_is(pilot, app, DeployScreen)
        s.query_one("#role", Select).value = "dev"
        await wait_for(lambda: s.verdict is not None and s.verdict.ok, pilot, "the guard's verdict")
        await ok(pilot, app)
        await confirm_open(pilot, app)
        await ok(pilot, app)
        await wait_for(lambda: app.dev is not None, pilot, "the dev box")
        await close_task(pilot, app)
        await goto(pilot, app, "2")                                   # scale the fleet up
        await pilot.press("s")
        form = await screen_is(pilot, app, FormScreen)
        form.query_one("#f-n", Input).value = "2"
        await ok(pilot, app)
        await confirm_open(pilot, app)
        await ok(pilot, app)
        await wait_for(lambda: len(app.fleet) == 2, pilot, "two workers")
        await close_task(pilot, app)
        first = app.fleet[0].id
        await selected(pilot, app, "fleet", first)
        await pilot.press("x")                                        # terminate one worker: typed phrase, only that worker goes
        await typed(pilot, app, "terminate")
        await wait_for(lambda: [i.id for i in app.fleet] != [first] and len(app.fleet) == 1, pilot, "one worker left")
        await close_task(pilot, app)
        await goto(pilot, app, "1")
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")                                        # terminate the dev box: its own phrase, takes the rest with it
        await typed(pilot, app, "stop dev")
        await wait_for(lambda: app.dev is None and not app.fleet, pilot, "everything closed")
        assert [c[1] for c in p.calls if c[0] == "terminate"][0] == first and len([c for c in p.calls if c[0] == "terminate"]) == 2
        assert not [c for c in p.calls if c[0] == "discard"], "checkpoints are on: nothing was discarded"
    drive(t, p)


def test_ui_on_the_real_controller_terminate_needs_the_phrase_and_a_failed_checkpoint_keeps_the_box(fleet):
    p = akash_on(fleet)
    dev = ddc.dev_box()["dseq"]

    async def t(app, pilot):
        await wait_for(lambda: app.dev is not None and len(app.fleet) == 2, pilot, "the fleet from the controller's records")
        fleet.checkpoint = "fail"                                     # the state repo is unreachable
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")
        await typed(pilot, app, "stop dev")
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to finish")
        assert len(fleet.open_dseqs()) == 3 and not deleted(fleet), "nothing was closed"
        await pilot.press("escape")
        await wait_for(lambda: not isinstance(app.screen, TaskScreen), pilot, "the task window to close")
        fleet.checkpoint = "ok"
        await selected(pilot, app, "inst", app.dev.id)
        await pilot.press("x")
        await typed(pilot, app, "stop dev")
        await wait_for(lambda: fleet.open_dseqs() == [], pilot, "everything closed after the checkpoint verified")
        await wait_for(lambda: app.dev is None and not app.fleet, pilot, "the UI to follow")
    drive(t, p, ready=lambda app: len(app.instances) >= 3)
    assert deps()[dev]["status"] == "closed"


def test_ui_offline_stays_alive_shows_the_last_known_state_and_refuses_new_spend(fleet):
    p = akash_on(fleet)

    async def t(app, pilot):
        await wait_for(lambda: len(app.fleet) == 2 and app.spend is not None, pilot, "the fleet")
        fleet.offline = True
        p._cache.clear()
        app.poll_network()
        app.poll_money()
        await wait_for(lambda: "connection failed" in (p.last_error or ""), pilot, "the provider to notice the outage")
        assert app.is_running and len(app.fleet) == 2 and app.dev is not None, "the last known state stays on screen"
        await pilot.press("d")                                        # new spend: refused by the guard, with the reason on screen
        s = await screen_is(pilot, app, DeployScreen)
        await wait_for(lambda: s.verdict is not None, pilot, "the verdict")
        assert not s.verdict.ok and "balance unknown" in s.verdict.text
        await pilot.press("escape")
        await wait_for(lambda: app.screen is not s, pilot, "the dialog to close")
        assert created(fleet) == 3
        fleet.offline = False
        p._cache.clear()
        app.action_refresh_all()
        await wait_for(lambda: app.spend is not None and app.spend.balance is not None, pilot, "the balance after the network is back")
    drive(t, p, ready=lambda app: len(app.instances) >= 3)


def test_ui_offline_is_visible_to_the_user_not_only_in_the_provider(fleet):
    p = akash_on(fleet)

    async def t(app, pilot):
        await wait_for(lambda: len(app.fleet) == 2 and app.spend is not None, pilot, "the fleet")
        assert not p.last_error and "provider" not in app.problems, "nothing is wrong while the API answers"
        fleet.offline = True
        p._cache.clear()
        app.poll_network()
        app.poll_money()
        await wait_for(lambda: "connection failed" in (p.last_error or ""), pilot, "the provider to notice the outage")
        await wait_for(lambda: "provider" in app.problems, pilot, "the outage to be reported as a problem (summary line / Monitor -> events)", timeout=6)   # polls run every second
        line = text_of(app.query_one("#summary"))
        assert "Akash API unreachable" in line and "connection failed" in line and "(cached data)" in line
        assert len(app.fleet) == 2, "the last known state stays on screen"
        fleet.offline = False
        p._cache.clear()
        app.action_refresh_all()
        await wait_for(lambda: not p.last_error and "provider" not in app.problems, pilot, "the warning to clear once the API answers again")
        assert "unreachable" not in text_of(app.query_one("#summary"))
    drive(t, p, ready=lambda app: len(app.instances) >= 3)


def test_ui_offline_on_the_simulated_provider_keeps_the_last_view_reports_the_problem_and_recovers():
    class Flaky(DemoProvider):
        down = False

        def _check(self):
            if self.down:
                raise ProviderError("network unreachable")

        def instances(self, refresh=False):
            if refresh:
                self._check()
            return super().instances(refresh)

        def spend(self):
            self._check()
            return super().spend()

        def terminate(self, instance_id, log=print, *, discard_workspace=False):
            self._check()
            return super().terminate(instance_id, log, discard_workspace=discard_workspace)

    p = Flaky(latency=0)

    async def t(app, pilot):
        known = [i.id for i in app.instances]
        p.down = True
        app.poll_network()
        app.poll_money()
        await wait_for(lambda: "network" in app.problems and "money" in app.problems, pilot, "both polls to report the outage")
        assert app.is_running and [i.id for i in app.instances] == known, "the last view stays on screen"
        assert "problem" in text_of(app.query_one("#summary"))
        await selected(pilot, app, "inst", app.fleet[0].id)
        await pilot.press("x")                                           # destructive actions still need their phrase, and fail safe while offline
        await typed(pilot, app, "terminate")
        await wait_for(lambda: isinstance(app.screen, TaskScreen) and app.screen.done, pilot, "the task to report its failure")
        assert len(app.fleet) == 3 and not [c for c in p.calls if c[0] == "discard"], "nothing was closed"
        await pilot.press("escape")
        await wait_for(lambda: not isinstance(app.screen, TaskScreen), pilot, "the task window to close")
        p.down = False
        app.action_refresh_all()
        await wait_for(lambda: not app.problems, pilot, "the problems to clear once the network is back")
    drive(t, p, ready=lambda app: len(app.instances) >= 5)


def test_the_outage_warning_is_short_and_leaks_no_paths_routes_or_keys(tmp_path):
    from clouddeck.tui.app import provider_warning
    key = "k" * 40
    win, posix = "\\".join(["C:", "Users", "someone", "x"]), "/".join(["", "home", "someone", "y"])
    raw = f"GET /v1/spend-rate: <urlopen error [Errno 11001] getaddrinfo failed> while reading {ddc.STATE_DIR / 'ledger.jsonl'} and {win} {posix} {key}\nTraceback (most recent call last):\n  File secret.py"
    w = provider_warning(raw)
    assert w.startswith("Akash API unreachable: ") and w.endswith("(cached data)") and "\n" not in w and len(w) < 140
    assert "/v1/" not in w and "Traceback" not in w and key not in w and "someone" not in w and str(ddc.STATE_DIR) not in w
    assert provider_warning("HTTP 401 GET /v1/balances: bad key").startswith("Akash API problem: bad key")      # not a network error: not called "unreachable"
    assert provider_warning("x" * 500).count("x") <= 70
    # two sources down at once (joined by '; '): the route never shows and the network cause is what the user sees
    both = "external deployments unavailable: GET /v1/deployments?skip=0&limit=10: connection failed; GET /v1/balances: connection failed"
    w = provider_warning(both)
    assert "/v1/" not in w and "GET" not in w and "connection failed" in w
    assert "connection failed" in provider_warning("external deployments unavailable: GET /v1/deployments?skip=0&limit=10: connection failed")


def test_the_provider_error_is_cleared_by_the_next_success_of_the_same_source(monkeypatch):
    p = providers.get("akash")
    monkeypatch.setattr(ddc, "balance_usd", lambda: (_ for _ in ()).throw(ddc.DdcError("GET /v1/balances: connection failed")))
    assert p.balance() is None and "connection failed" in p.last_error
    p._note("ledger", "ledger is damaged")
    monkeypatch.setattr(ddc, "balance_usd", lambda: 12.5)
    p._cache.clear()
    assert p.balance() == 12.5 and p.last_error == "ledger is damaged", "only the balance error went away"
    p._note("ledger")
    assert p.last_error == ""
