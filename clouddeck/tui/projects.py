"""Projects tab: repositories and folders, their workspaces on the dev box, environments, Claude Code sessions."""
from __future__ import annotations

import time
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Label, Static

from ..engine import ddc
from ..providers.base import Spec
from ..service import Project, Session, Workspace
from .compute import DeployScreen
from .widgets import Col, ConfirmScreen, Field, FilterTable, FormScreen, Pane, SelectScreen, kv_table

NO_ENV = "(none)"


def env_spec(app: Any, env: dict[str, Any]) -> Spec | None:
    presets = app.provider.presets()
    if env.get("profile") in presets:
        return presets[env["profile"]]
    if env.get("cpu"):
        return Spec(cpu=int(env["cpu"]), mem_gb=int(env.get("mem_gb", 16)), disk_gb=int(env.get("disk_gb", 50)), gpu=int(env.get("gpu", 0)),
                    gpu_model=str(env.get("gpu_model", "")), max_usd_hr=float(env.get("max_usd_hr", 0)), hours=int(env.get("hours", 4)))
    return None


class ProjectsPane(Pane):
    primary = "#proj"
    BINDINGS = [
        Binding("a", "add", "Add project"), Binding("delete", "forget", "Forget"), Binding("e", "environment", "Environment"),
        Binding("n", "new_session", "New Claude"), Binding("c", "continue_session", "Continue"), Binding("k", "kill_session", "Kill session", show=False),
        Binding("s", "seed", "Seed to box", show=False), Binding("D", "deploy_env", "Deploy env", show=False), Binding("tab", "swap", "Switch table", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ws: dict[str, Workspace] = {}
        self.sessions: list[Session] = []
        self.fetched = 0.0
        self.fetching = False
        self.error = ""

    def compose(self) -> ComposeResult:
        with Horizontal(classes="split"):
            with Vertical(classes="main-col"):
                yield Label("Projects  (repos and folders restored onto the dev box)", classes="section")
                yield FilterTable([Col("Project", "name"), Col("Kind", "kind"), Col("On the dev box", "box"), Col("Environment", "env"), Col("GitHub", "gh"), Col("Local path", "path")],
                                  id="proj", empty="No projects. Press a to add a repository or folder.")
            with Vertical(classes="detail", id="proj-side"):
                yield Label("Claude Code sessions (tmux windows on the dev box)", classes="section")
                yield FilterTable([Col("#", "i", right=True), Col("Window", "name"), Col("Running", "cmd"), Col("Folder", "path")], id="sess",
                                  empty="No dev box, or no sessions yet. n starts one in the selected project.")
                yield Static(id="proj-detail")

    def palette(self) -> list[SystemCommand]:
        return [SystemCommand("Add project…", "Add a repository or folder to config.toml", self.action_add),
                SystemCommand("New Claude Code session…", "Start Claude Code in a project on the dev box", self.action_new_session),
                SystemCommand("Continue last Claude session…", "claude --continue in a project", self.action_continue_session)]

    # ---- data
    def on_show(self) -> None:
        self.refresh_view("manual")

    def refresh_view(self, kind: str) -> None:
        if not self.is_mounted or kind in ("events", "probes", "money", "idle"):
            return
        self._fill()
        app: Any = self.app
        stale = time.time() - self.fetched > 25
        if app.dev and app.provider.has("workspace") and not self.fetching and (kind in ("manual", "all") or (kind == "instances" and stale)):
            self.fetch()
        if not app.dev and (self.ws or self.sessions):
            self.ws, self.sessions = {}, []
            self._fill()

    def fetch(self) -> None:
        app: Any = self.app
        dev = app.dev
        if not dev:
            return
        self.fetching = True

        def work() -> tuple[dict[str, Workspace], list[Session]]:
            return app.service.workspaces(dev), app.service.sessions(dev)

        def done(res: tuple[dict[str, Workspace], list[Session]]) -> None:
            self.fetching, self.fetched, self.error = False, time.time(), ""
            self.ws, self.sessions = res
            self._fill()

        def fail(e: BaseException) -> None:
            self.fetching, self.error = False, str(e)[:160]
            self._fill()
        app.bg(work, done, "Workspaces", fail=fail)

    def _fill(self) -> None:
        app: Any = self.app
        rows = []
        for p in app.service.projects():
            w = self.ws.get(p.name)
            if not app.dev:
                box = Text("dev box off", style="dim")
            elif self.fetching and not self.ws:
                box = Text("checking…", style="dim")
            elif w and w.kind == "git":
                box = Text(f"{w.branch}@{w.head}" + (f"  ✎{w.dirty}" if w.dirty else "  clean"), style="yellow" if w.dirty else "green")
            elif w:
                box = Text(f"folder {w.size_mb} MB", style="green")
            else:
                box = Text("not seeded yet", style="yellow")
            rows.append((p.name, [Text(p.name, style="bold"), p.kind, box, p.environment or Text("—", style="dim"), (p.github + (" ⇡push" if p.push else "")) if p.github else "—",
                                  Text(p.path, style="dim")]))
        self.query_one("#proj", FilterTable).set_rows(rows)
        self.query_one("#sess", FilterTable).set_rows(
            [(s.name, [str(s.index), Text(s.name, style="bold green" if s.active else "bold"), s.command, Text(s.path, style="dim")]) for s in self.sessions])
        self._detail()

    @on(FilterTable.Highlighted)
    def _hl(self, e: FilterTable.Highlighted) -> None:
        self._detail()

    def _project(self) -> Project | None:
        key = self.query_one("#proj", FilterTable).selected_key
        return next((p for p in self.app.service.projects() if p.name == key), None)  # type: ignore[attr-defined]

    def _session(self) -> Session | None:
        key = self.query_one("#sess", FilterTable).selected_key
        return next((s for s in self.sessions if s.name == key), None)

    def _detail(self) -> None:
        p = self._project()
        box = self.query_one("#proj-detail", Static)
        if not p:
            box.update(Text(""))
            return
        w = self.ws.get(p.name)
        rows: list[tuple[str, Any]] = [("Project", Text(p.name, style="bold")), ("Kind", p.kind), ("Local", p.path), ("GitHub", p.github or "none")]
        if p.kind == "repo":
            rows.append(("Push from cloud", "yes (dedicated deploy key)" if p.push else Text("no (push URL disabled on the box)", style="dim")))
        rows.append(("Environment", p.environment or "—"))
        rows.append(("Workspace", f"/work/{p.name}" + (f"  ({w.branch}@{w.head}, {w.dirty} changed)" if w and w.kind == "git" else "")))
        if self.error:
            rows.append(("Box", Text(self.error, style="red")))
        rows.append(("", Text("Enter session · n new · c continue · e environment · D deploy env · s seed · a add · Del forget", style="dim")))
        box.update(kv_table(rows))

    # ---- actions
    def action_swap(self) -> None:
        proj, sess = self.query_one("#proj", FilterTable), self.query_one("#sess", FilterTable)
        (sess if proj.table.has_focus else proj).focus_table()

    def action_add(self) -> None:
        app: Any = self.app
        envs = [(NO_ENV, "")] + [(k, k) for k in app.service.environments() if not k.startswith("profile ")]
        form = FormScreen("Add project", [Field("name", "Name (lowercase, used as /work/<name>)", "text"), Field("path", "Folder on this PC", "text"),
                                          Field("github", "GitHub owner/repo (optional)", "text", required=False, hint="Needed for push access from the cloud."),
                                          Field("push", "Allow pushing to GitHub from the dev box", "switch", False, required=False),
                                          Field("environment", "Environment (optional)", "select", "", options=envs, required=False)], submit="Add",
                          intro="Writes to config.toml (a .bak copy is kept). Nothing is uploaded until the dev box seeds it.")
        app.push_screen(form, self._added)

    def _added(self, v: dict[str, Any] | None) -> None:
        if not v:
            return
        app: Any = self.app
        app.bg(lambda: app.service.add_project(v["name"], v["path"], v.get("github") or "", bool(v.get("push")), v.get("environment") or ""),
               lambda p: (app.notify(f"Added {p.name}. Press s to copy it to the running dev box.", title="Project added"), self._fill()), "Add project")

    def action_forget(self) -> None:
        p = self._project()
        if not p:
            return
        app: Any = self.app
        body = Text(f"Removes '{p.name}' from config.toml (a .bak copy is kept).\nThe workspace on the dev box and existing checkpoints are NOT touched.")
        app.push_screen(ConfirmScreen("Forget project?", body, "Forget", danger=True),
                        lambda ok: app.bg(lambda: app.service.remove_project(p.name), lambda _: (app.notify(f"Forgot {p.name}"), self._fill()), "Forget project") if ok else None)

    def action_environment(self) -> None:
        p = self._project()
        if not p:
            return
        app: Any = self.app
        envs = app.service.environments()
        items = [(NO_ENV, "", "no environment")] + [(k, k, ", ".join(f"{a}={b}" for a, b in v.items())) for k, v in envs.items()]
        app.push_screen(SelectScreen(f"Environment for {p.name}", items),
                        lambda choice: app.bg(lambda: app.service.set_project_environment(p.name, choice), lambda _: self._fill(), "Set environment") if choice is not None else None)

    def action_deploy_env(self) -> None:
        p = self._project()
        app: Any = self.app
        if not p:
            return
        env = app.service.environments().get(p.environment) if p.environment else None
        spec = env_spec(app, env) if env else None
        if not spec:
            app.notify("This project has no environment with a size. Press e to pick one.", severity="warning")
            return
        role = "dev" if app.provider.has("workspace") and not app.dev else "box"
        spec = spec.with_(role=role, hours=int(ddc.CFG.get("max_runtime_hours", 8)) if role == "dev" else spec.hours)
        from .compute import ComputePane
        pane = next((x for x in app.panes if isinstance(x, ComputePane)), None)
        if pane:
            app.push_screen(DeployScreen(spec, title=f"Deploy environment for {p.name}"), pane._deploy_chosen)

    def _need_dev(self) -> Any:
        dev = self.app.dev  # type: ignore[attr-defined]
        if not dev:
            self.app.notify("The dev box is not running. Start it in Compute (d).", severity="warning")  # type: ignore[attr-defined]
        return dev

    def _start(self, resume: bool) -> None:
        p = self._project()
        app: Any = self.app
        dev = self._need_dev()
        if not p or not dev:
            return
        if p.name not in self.ws:
            app.notify(f"'{p.name}' is not on the dev box yet. Press s to seed it first.", severity="warning")
            return
        app.bg(lambda: app.service.new_session(dev, p.name, resume), lambda name: (app.notify(f"Started {name}", timeout=3), app.attach(name)), "New Claude session")

    def action_new_session(self) -> None:
        self._start(False)

    def action_continue_session(self) -> None:
        self._start(True)

    def action_kill_session(self) -> None:
        s, dev = self._session(), self._need_dev()
        app: Any = self.app
        if not s or not dev:
            return
        app.push_screen(ConfirmScreen("Close this Claude session?", Text(f"Closes tmux window '{s.name}'. Claude's conversation history stays on the box and `claude --continue` can resume it."),
                                      "Close window", danger=True),
                        lambda ok: app.bg(lambda: app.service.kill_session(dev, s.name), lambda _: self.fetch(), "Close session") if ok else None)

    def action_seed(self) -> None:
        p, dev = self._project(), self._need_dev()
        app: Any = self.app
        if not p or not dev:
            return
        app.push_screen(ConfirmScreen(f"Copy {p.name} to the dev box?", Text("Uploads the folder (secret-checked; .env, keys and wallets are refused). Only allowed when it is not on the box yet."), "Seed"),
                        lambda ok: app.run_task(f"Seed {p.name}", lambda log: app.service.seed_missing(dev, p.name)) if ok else None)

    @on(FilterTable.Selected)
    def _enter(self, e: FilterTable.Selected) -> None:
        app: Any = self.app
        if e.table.id == "sess":
            if self._need_dev():
                app.attach(e.key)
            return
        p = self._project()
        if not p or not self._need_dev():
            return
        win = app.service.window_name(p.name)
        if any(s.name == win for s in self.sessions):
            app.attach(win)
        else:
            self._start(False)
