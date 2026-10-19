"""The CloudDeck application: shared live data, background polling, task runner, navigation."""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from .. import engine, fmt
from ..engine import ddc
from ..providers.base import IdleState, Instance, Provider, ProviderError, Spec, Spend, Verdict
from ..remote import scrub
from ..service import Probe, Service
from .safety import discard_prompt, is_discard_refusal
from .widgets import ConfirmScreen, InfoScreen, Pane, TaskScreen

HELP = """\
[b]Navigation[/b]
  1-5        Compute · Workers · Projects · Monitor · Recovery
  Ctrl+P     command palette (every action, searchable)
  /          search the table you are in      Esc  clear
  r          refresh everything now           ?    this help
  q          quit (leases keep running; nothing is closed)

[b]Compute[/b]   d deploy · h hardware · z resize · x terminate · Enter open shell · i/p/g sub-tabs · f GPU only · s sort · u reload market
[b]Workers[/b]   + add one · s scale to N · d dispatch workload · Enter inspect · x terminate · h heal · a shell
[b]Projects[/b]  a add · Delete forget · e environment · n new Claude session · c continue session · k kill session · s seed · D deploy env · Enter attach
[b]Monitor[/b]   l choose log source · f follow log
[b]Recovery[/b]  c checkpoint now · v verify · a reconnect · h heal · s start/restore · z snooze idle · t idle settings · X stop everything

[b]Safety[/b]
  • Only leases created by this system (state-recorded, named ddc-*) can ever be closed. Other deployments are shown read-only.
  • Closing the dev box first makes a final checkpoint and verifies it on this PC; if that fails the box stays up.
  • Every terminate / resize / scale-down lists what is destroyed, what it cost and the checkpoint status, and needs a typed phrase.
  • Every deploy / resize / scale-up shows the spend guard's verdict (ceiling = price cap × hours) and is refused when the guard says no.
  • Every price cap is written into the order itself, so no bid above it can win.
  • No signing keys or Akash API key ever leave this PC.
"""

HINTS = [
    ("AKASH_API_KEY missing", "Put AKASH_API_KEY=… into the secrets.env shown in `ddc status`; the key never leaves this PC."),
    ("HTTP 401", "Akash rejected the API key (401). Create a new key in Akash Console and update secrets.env."),
    ("no acceptable bids", "No provider bid under your price cap. Raise the cap, shrink the spec, or retry in a minute."),
    ("another ddc command holds", "Another ddc operation (start/stop/watchdog) is running. Wait for it; see the Monitor → ddc.log."),
    ("ssh never came up", "The lease started but SSH did not answer; the provider may be slow. The lease was closed, retry."),
    ("would exceed budget", "The budget guard is enabled in config.toml and blocks this spend."),
    ("would breach reserve", "The account balance would drop below reserve_usd in config.toml."),
    ("max_total_leases", "config.toml limits how many leases may run at once (max_total_leases)."),
    ("max_workers", "config.toml limits how many workers may run at once (max_workers)."),
    ("balance unknown", "The account balance could not be read, so the budget cannot be checked. Check AKASH_API_KEY and the network."),
    ("will NOT create leases", "The ddc state file is damaged: nothing new is started until it is resolved. `clouddeck doctor` shows where it is; `ddc reconcile` repairs it."),
    ("checkpoints are disabled", "Nothing was closed: state_repo is empty, so the dev box cannot be saved. Set state_repo (then `ddc install`), or start the action again: the dialog then asks you to type `discard workspace` (Esc cancels)."),
    ("not reachable", "That instance is not running (yet). Wait until its state is 'running'."),
]


def describe(e: BaseException) -> str:
    msg = str(e) or type(e).__name__
    if not isinstance(e, (ProviderError, engine.DdcError)):
        msg = f"{type(e).__name__}: {msg}"
    msg = msg.strip()
    hint = next((h for needle, h in HINTS if needle in msg), "")
    return msg[:700] + (f"\n→ {hint}" if hint else "")


_NET_WORDS = re.compile(r"connect|timed? ?out|unreachable|urlopen|resolve|refused|reset|HTTP 5\d\d", re.IGNORECASE)
_PATHS = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|~[\\/]|/(?:home|Users|root|tmp|var|mnt|opt|etc)/)\S*")


def provider_warning(err: str) -> str:
    """One short summary-line warning for a provider's last_error: first line only, no API route, no folder names, nothing key-shaped."""
    line = scrub(err.strip().splitlines()[0] if err.strip() else "")
    # several data sources can be down at once ("a; b"): show the one that names the network problem, else the first
    parts = [p for p in re.split(r";\s+", line) if p]
    line = next((p for p in parts if _NET_WORDS.search(p)), parts[0] if parts else "")
    line = re.sub(r"(?:HTTP \d+ )?(?:GET|POST|DELETE|PUT|HEAD) \S+?:(?=\s|$)\s*", "", line)   # the API route, wherever it sits
    line = re.sub(r"(?:GET|POST|DELETE|PUT|HEAD) /\S*", "", line)
    line = re.sub(r"[A-Za-z0-9_\-]{24,}", "<hidden>", _PATHS.sub("<path>", line)).strip()
    reason = line if len(line) <= 70 else line[:69] + "…"
    return f"Akash API {'unreachable' if _NET_WORDS.search(err) else 'problem'}: {reason or 'no details'} (cached data)"


class Summary(Static):
    pass


class CloudDeckApp(App):
    TITLE = "CloudDeck"
    CSS_PATH = "clouddeck.tcss"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (100, "-normal"), (150, "-wide")]
    VERTICAL_BREAKPOINTS = [(0, "-short"), (32, "-tall")]
    BINDINGS = [
        Binding("1", "tab('compute')", "Compute", show=False), Binding("2", "tab('workers')", "Workers", show=False), Binding("3", "tab('projects')", "Projects", show=False),
        Binding("4", "tab('monitor')", "Monitor", show=False), Binding("5", "tab('recovery')", "Recovery", show=False),
        Binding("r", "refresh_all", "Refresh"), Binding("question_mark", "help", "Help"), Binding("q", "request_quit", "Quit"),
    ]

    def __init__(self, provider: Provider, poll_s: float = 5, net_s: float = 45, probe_s: float = 20, money_s: float = 30) -> None:
        super().__init__()
        self.provider = provider
        self.service = Service(provider)
        self.poll_s, self.net_s, self.probe_s, self.money_s = poll_s, net_s, probe_s, money_s
        self.instances: list[Instance] = []
        self.probes: dict[str, Probe] = {}
        self.history: dict[str, deque[float]] = {}
        self.spend: Spend | None = None
        self.idle: IdleState | None = None
        self.problems: dict[str, str] = {}
        self.events: deque[str] = deque(maxlen=800)
        self.busy = 0
        self.panes: list[Pane] = []
        self._inflight: set[str] = set()
        self._tasks: list[TaskScreen] = []
        self._ui_thread = threading.get_ident()
        self._prev_log: Callable[[str], None] = ddc.log
        self.retry_discard: Callable[[], None] | None = None   # set by confirm_destroy: re-run the last destructive action with discard_workspace
        self.ui_errors: list[Exception] = []   # callbacks that raised on the UI thread (tests assert this stays empty)
        self.sub_title = provider.label

    # ------------------------------------------------------------ layout
    def compose(self) -> ComposeResult:
        from .compute import ComputePane
        from .monitor import MonitorPane
        from .projects import ProjectsPane
        from .recovery import RecoveryPane
        from .workers import WorkersPane
        yield Header(show_clock=True)
        yield Summary(id="summary")
        with TabbedContent(initial="compute", id="tabs"):
            with TabPane("1 Compute", id="compute"):
                yield ComputePane()
            with TabPane("2 Workers", id="workers"):
                yield WorkersPane()
            with TabPane("3 Projects", id="projects"):
                yield ProjectsPane()
            with TabPane("4 Monitor", id="monitor"):
                yield MonitorPane()
            with TabPane("5 Recovery", id="recovery"):
                yield RecoveryPane()
        yield Footer()

    def on_mount(self) -> None:
        self._prev_log = engine.set_log_hook(lambda line: self.ui(self._on_ddc_line, line))
        st = ddc.read_json(ddc.STATE_DIR / "clouddeck.json", {})
        try:
            self.theme = st.get("theme") or "tokyo-night"
        except Exception:
            pass
        self.log_event(f"CloudDeck started · provider {self.provider.label} · config {ddc.CONFIG_FILE}")
        self.set_interval(self.poll_s, lambda: self.poll("local", lambda: self.provider.instances(False), self._got_instances))
        self.set_interval(self.net_s, self.poll_network)
        self.set_interval(self.probe_s, self.poll_probes)
        self.set_interval(self.money_s, self.poll_money)
        self.set_interval(1.0, self.refresh_summary)
        self.poll_network()
        self.poll_money()
        self.call_after_refresh(self._focus_active)

    def on_unmount(self) -> None:
        engine.restore_log(self._prev_log)   # the controller must not keep logging into a closed UI

    def watch_theme(self, theme: str) -> None:
        try:
            st = ddc.read_json(ddc.STATE_DIR / "clouddeck.json", {})
            st["theme"] = theme
            ddc.write_json(ddc.STATE_DIR / "clouddeck.json", st)
        except OSError:
            pass

    # ------------------------------------------------------------ threading helpers
    def ui(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> None:
        """Run fn on the UI thread from anywhere."""
        if threading.get_ident() == self._ui_thread:
            fn(*args, **kw)
            return
        try:
            self.call_from_thread(fn, *args, **kw)
        except Exception as e:  # the app is shutting down, or `fn` failed on the UI thread: never let a worker thread die over it, but keep the error
            self.ui_errors.append(e)

    def bg(self, fn: Callable[[], Any], done: Callable[[Any], None] | None = None, title: str = "", fail: Callable[[BaseException], None] | None = None) -> None:
        """Run a blocking call in a thread; show errors clearly; call `done(result)` on the UI thread."""
        self.busy += 1

        def job() -> None:
            try:   # `busy` drops last, after the result (or error) was delivered: "busy == 0" means no callback is still on its way
                try:
                    res = fn()
                except Exception as e:
                    self.ui(self._bg_failed, title, e, fail)
                    return
                if done:
                    self.ui(done, res)
            finally:
                self.ui(self._bg_end)
        self.run_worker(job, thread=True, exit_on_error=False)

    def _bg_end(self) -> None:
        self.busy = max(0, self.busy - 1)

    def _bg_failed(self, title: str, e: BaseException, fail: Callable[[BaseException], None] | None) -> None:
        msg = describe(e)
        self.log_event(f"ERROR {title}: {msg}")
        if fail:
            fail(e)
        else:
            self.notify(msg, title=f"{title} failed" if title else "Error", severity="error", timeout=12)

    def check_guard(self, spec: Spec, n: int, replacing: Instance | None, then: Callable[[Verdict], None]) -> None:
        """Ask the provider's spend guard (off the UI thread: it may call the network), then hand the verdict to `then`.
        If the guard cannot be evaluated the answer is no: nothing is offered that it has not approved."""
        def failed(e: BaseException) -> None:
            then(Verdict(False, f"could not check the spend guard: {describe(e)}", spec.max_usd_hr * spec.hours * n))
        self.bg(lambda: self.provider.guard(spec, n, replacing), then, "Spend guard", fail=failed)

    def run_task(self, title: str, fn: Callable[[Callable[[str], None]], Any], done: Callable[[Any], None] | None = None) -> None:
        """Long operation with a live log window (Esc hides it, it keeps running)."""
        screen = TaskScreen(title)
        self._tasks.append(screen)
        self.push_screen(screen)
        self.busy += 1
        self.log_event(f"▶ {title}")

        def log(line: str) -> None:
            self.ui(screen.append, line)

        def job() -> None:
            try:
                res = fn(log)
            except Exception as e:
                msg = describe(e)
                self.ui(self.log_event, f"✗ {title}: {msg}")
                self.ui(screen.finish, False, msg)
                self.ui(self.notify, msg, title=f"{title} failed", severity="error", timeout=15)
                if is_discard_refusal(e):   # nothing was closed: let the user choose, by typing, to discard the workspace
                    self.ui(discard_prompt, self, msg.splitlines()[0])
            else:
                self.ui(self.log_event, f"✓ {title}")
                self.ui(screen.finish, True, "done")
                self.ui(self.notify, title, title="Done", timeout=5)
                if done:
                    self.ui(done, res)
            finally:
                self.ui(self._task_end, screen)
        self.run_worker(job, thread=True, exit_on_error=False)

    def _task_end(self, screen: TaskScreen) -> None:
        self.busy = max(0, self.busy - 1)
        self.retry_discard = None   # a later, unrelated failure must never re-run an old destructive action
        self.poll("local", lambda: self.provider.instances(False), self._got_instances)
        self.poll_network()
        self.poll_money()

    def _on_ddc_line(self, line: str) -> None:
        self.log_event(line)
        for t in reversed(self._tasks):
            if not t.done:
                t.append(line)
                break

    def log_event(self, line: str) -> None:
        self.events.append(line if line[:1] == "[" else f"[{time.strftime('%H:%M:%S')}] {line}")
        for p in self.panes:
            p.refresh_view("events")

    # ------------------------------------------------------------ polling
    def poll(self, name: str, fn: Callable[[], Any], apply: Callable[[Any], None]) -> None:
        if name in self._inflight:
            return
        self._inflight.add(name)

        def job() -> None:
            try:
                res = fn()
            except Exception as e:
                self.ui(self._set_problem, name, describe(e))
            else:
                self.ui(self._set_problem, name, "")
                self.ui(apply, res)
                self.ui(self._provider_note)
            finally:
                self._inflight.discard(name)
        self.run_worker(job, thread=True, exit_on_error=False)

    def _set_problem(self, name: str, msg: str) -> None:
        if msg and self.problems.get(name) != msg:
            self.log_event(f"{name}: {msg}")
        if msg:
            self.problems[name] = msg
        else:
            self.problems.pop(name, None)
        self.refresh_summary()

    def _provider_note(self) -> None:
        """A provider that swallows an outage and serves cached data (Akash: last_error) still gets a visible warning; the next success clears it."""
        err = getattr(self.provider, "last_error", "") or ""
        self._set_problem("provider", provider_warning(err) if err else "")

    def poll_network(self) -> None:
        self.poll("network", lambda: self.provider.instances(True), self._got_instances)

    def poll_probes(self) -> None:
        if self.screen is not self.screen_stack[0] or not self.instances:   # a dialog is open: do not stack SSH sessions behind it
            return
        try:
            if self.query_one("#tabs", TabbedContent).active in ("projects", "recovery"):
                return
        except NoMatches:   # the timer fired while the app was shutting down: the tabs are gone and there is nothing to probe
            return
        self.poll("probes", lambda: self.service.probe_all(self.instances), self._got_probes)

    def poll_money(self) -> None:
        self.poll("money", self.provider.spend, self._got_spend)
        if self.provider.has("snooze") and self.dev:
            self.poll("idle", self.provider.idle_state, self._got_idle)

    def _got_instances(self, items: list[Instance]) -> None:
        self.instances = items
        alive = {i.id for i in items}
        for k in [k for k in self.history if k not in alive]:
            self.history.pop(k)
            self.probes.pop(k, None)
        self._notify("instances")

    def _got_probes(self, probes: dict[str, Probe]) -> None:
        self.probes.update(probes)
        for k, p in probes.items():
            if p.ok and p.cpu_frac is not None:
                self.history.setdefault(k, deque(maxlen=90)).append(p.cpu_frac)
        self._notify("probes")

    def _got_spend(self, s: Spend) -> None:
        self.spend = s
        self._notify("money")

    def _got_idle(self, s: IdleState | None) -> None:
        self.idle = s
        self._notify("idle")

    def _notify(self, kind: str) -> None:
        for p in self.panes:
            p.refresh_view(kind)
        self.refresh_summary()

    def action_refresh_all(self) -> None:
        self.notify("refreshing…", timeout=2)
        self.poll("local", lambda: self.provider.instances(False), self._got_instances)
        self.poll_network()
        self.poll_probes()
        self.poll_money()
        for p in self.panes:
            p.refresh_view("manual")

    # ------------------------------------------------------------ derived data
    @property
    def dev(self) -> Instance | None:
        return next((i for i in self.instances if i.role == "dev" and i.state == "running"), None)

    @property
    def fleet(self) -> list[Instance]:
        return [i for i in self.instances if i.role == "worker"]

    def instance(self, key: str | None) -> Instance | None:
        return next((i for i in self.instances if i.id == key), None)

    def refresh_summary(self) -> None:
        mine = [i for i in self.instances if i.managed]
        run = [i for i in mine if i.state == "running"]
        s = self.spend
        t = Text()
        t.append(" ☁ " + self.provider.label, style="bold")
        t.append("  │  balance ", style="dim")
        bal = s.balance if s else None
        t.append(fmt.money(bal), style="bold green" if bal and bal > 15 else "bold red" if bal is not None else "bold")
        t.append("  │  ", style="dim")
        dev = self.dev
        t.append("● dev" if dev else "○ no dev box", style="green" if dev else "yellow")
        t.append(f" + {len(self.fleet)} workers", style="")
        t.append(f"  │  {len(run)} running  ", style="dim")
        t.append(fmt.rate(sum(i.usd_hr for i in run)), style="bold")
        if s:
            t.append("  │  today ", style="dim")
            t.append(fmt.money(s.today), style="bold")
        if self.provider.id == "demo":
            t.append("  │  DEMO — simulated, nothing real is touched", style="bold magenta")
        if self.busy:
            t.append(f"  │  ⟳ {self.busy} working", style="bold yellow")
        if self.problems.get("provider"):
            t.append(f"  │  ⚠ {self.problems['provider']}", style="bold red")
        n = len(self.problems) - ("provider" in self.problems)
        if n:
            t.append(f"  │  ⚠ {n} problem{'s' * (n > 1)} (Monitor → events)", style="bold red")
        try:
            self.query_one("#summary", Summary).update(t)
        except Exception:
            pass

    # ------------------------------------------------------------ navigation / actions
    def _focus_active(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        for p in self.panes:
            if p.parent and getattr(p.parent, "id", None) == active:
                p.focus_primary()

    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = name
        self.call_after_refresh(self._focus_active)

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, e: TabbedContent.TabActivated) -> None:
        e.stop()
        if e.tabbed_content.id == "tabs":
            self.call_after_refresh(self._focus_active)
            self.poll_probes()

    def action_help(self) -> None:
        self.push_screen(InfoScreen("CloudDeck keys", Text.from_markup(HELP)))

    def action_request_quit(self) -> None:
        body = "Leases keep running and billing after CloudDeck exits; nothing is closed by quitting."
        if self.busy:
            body = f"{self.busy} operation(s) still running. They will be interrupted.\n\n" + body
        self.push_screen(ConfirmScreen("Quit CloudDeck?", body, "Quit"), lambda ok: self.exit() if ok else None)

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        nav = [("Compute", "compute"), ("Workers", "workers"), ("Projects", "projects"), ("Monitor", "monitor"), ("Recovery", "recovery")]
        for label, tab in nav:
            yield SystemCommand(f"Go to {label}", f"Open the {label} tab", lambda t=tab: self.action_tab(t))
        yield SystemCommand("Refresh everything", "Re-read instances, spend, probes", self.action_refresh_all)
        yield SystemCommand("Help / keys", "Show the key reference", self.action_help)
        for p in self.panes:
            yield from getattr(p, "palette", lambda: [])()

    # ------------------------------------------------------------ interactive sessions
    def interactive(self, fn: Callable[[], Any], what: str) -> None:
        """Give the terminal to an interactive command, then come back.  Errors are caught *inside* the suspended block:
        an exception escaping it would leave the TUI suspended."""
        err = ""
        try:
            with self.suspend():
                try:
                    fn()
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
        except Exception as e:   # SuspendNotSupported (e.g. headless / web)
            err = str(e) or type(e).__name__
        if err:
            self.notify(f"Cannot open {what} here ({err}).", severity="warning", timeout=12)
        self.action_refresh_all()

    def attach(self, window: str = "") -> None:
        """Hand the terminal to an ssh+tmux session on the dev box; CloudDeck resumes when it ends."""
        dev = self.dev
        if not dev:
            self.notify("No dev box is running.", severity="warning")
            return

        def run() -> None:
            if self.provider.id == "akash":
                ddc.cmd_attach(argparse.Namespace(window=window or None))   # reconnects automatically on a dropped link
            else:
                subprocess.call(self.service.attach_command(dev, window))
        self.interactive(run, "the Claude Code session")

    def shell(self, inst: Instance) -> None:
        try:
            argv = self.provider.ssh_command(inst, tty=True)
        except ProviderError as e:
            self.notify(describe(e), severity="warning")
            return
        self.interactive(lambda: subprocess.call(argv), "a shell")

    def active_pane(self) -> Pane | None:
        active = self.query_one("#tabs", TabbedContent).active
        return next((p for p in self.panes if p.parent and getattr(p.parent, "id", None) == active), None)


def make_app(provider_name: str | None = None) -> CloudDeckApp:
    from .. import providers
    name = provider_name or os.environ.get("CLOUDDECK_PROVIDER") or "akash"
    return CloudDeckApp(providers.get(name))
