"""Reusable building blocks: filterable table, searchable select, confirm / form modals, streaming task log."""
from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, OptionList, RichLog, Select, Static, Switch
from textual.widgets.option_list import Option


def cell(v: Any, style: str = "") -> Text:
    """Cells are Text so provider data such as '[abc123]' can never be parsed as markup."""
    return v if isinstance(v, Text) else Text(str(v), style=style)


def kv_table(rows: Sequence[tuple[str, Any]], key_style: str = "dim") -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style=key_style, no_wrap=True)
    t.add_column(overflow="fold")
    for k, v in rows:
        t.add_row(k, v if isinstance(v, (Text, Table)) else Text(str(v)))
    return t


# ---------------------------------------------------------------------------------------------- table
@dataclass
class Col:
    label: str
    key: str
    width: int | None = None
    right: bool = False


class FilterTable(Vertical):
    """DataTable with `/` search, sortable headers and flicker-free refresh that keeps the selected row."""

    BINDINGS = [Binding("slash", "search", "Search", show=True), Binding("escape", "clear_search", "Clear", show=False)]

    class Highlighted(Message):
        def __init__(self, table: FilterTable, key: str | None) -> None:
            super().__init__()
            self.table, self.key = table, key

    class Selected(Message):
        def __init__(self, table: FilterTable, key: str) -> None:
            super().__init__()
            self.table, self.key = table, key

    def __init__(self, columns: Sequence[Col], *, placeholder: str = "Search…  (Esc clears)", empty: str = "Nothing here yet.", **kw: Any) -> None:
        super().__init__(**kw)
        self.cols = list(columns)
        self.placeholder = placeholder
        self.empty_text = empty
        self._rows: list[tuple[str, list[Any], list[Any]]] = []   # key, cells, sort values
        self._shown: list[str] = []
        self._needle = ""
        self._sort: tuple[int, bool] | None = None
        self.filter_fn: Callable[[str, list[Any]], bool] | None = None   # extra row filter, e.g. "GPU only"

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self.placeholder, id="q", classes="filter-input")
        t = DataTable(cursor_type="row", zebra_stripes=True, id="t")
        yield t
        yield Static(self.empty_text, classes="empty", id="empty")

    def on_mount(self) -> None:
        t = self.table
        for c in self.cols:
            t.add_column(Text(c.label, justify="right" if c.right else "left"), key=c.key, width=c.width)
        self.query_one("#q", Input).display = False

    @property
    def table(self) -> DataTable:
        return self.query_one("#t", DataTable)

    @property
    def selected_key(self) -> str | None:
        t = self.table
        if not t.row_count:
            return None
        try:
            return str(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        except Exception:
            return None

    def focus_table(self) -> None:
        self.table.focus()

    def set_rows(self, rows: Iterable[tuple[str, list[Any]] | tuple[str, list[Any], list[Any]]]) -> None:
        self._rows = [(r[0], [cell(c) for c in r[1]], list(r[2]) if len(r) > 2 else [c.plain.lower() for c in map(cell, r[1])]) for r in rows]  # type: ignore[misc]
        self._apply()

    def _visible(self) -> list[tuple[str, list[Any], list[Any]]]:
        rows = self._rows
        if self._needle:
            toks = self._needle.lower().split()
            rows = [r for r in rows if all(any(t in c.plain.lower() for c in r[1]) for t in toks)]
        if self.filter_fn:
            rows = [r for r in rows if self.filter_fn(r[0], r[1])]
        if self._sort:
            i, rev = self._sort
            rows = sorted(rows, key=lambda r: (r[2][i] is None, r[2][i]) if i < len(r[2]) else (0, 0), reverse=rev)
        return rows

    def _apply(self) -> None:
        if not self.is_mounted or not self.table.columns:
            return
        t = self.table
        rows = self._visible()
        keep = self.selected_key
        keys = [r[0] for r in rows]
        if keys == self._shown:   # same rows, same order: just patch changed cells
            for key, cells, _ in rows:
                for c, v in zip(self.cols, cells):
                    cur = t.get_cell(key, c.key)
                    if not (isinstance(cur, Text) and cur.plain == v.plain and cur.style == v.style):
                        t.update_cell(key, c.key, v, update_width=True)
        else:
            t.clear()
            for key, cells, _ in rows:
                t.add_row(*cells, key=key)
            if keep in keys:
                t.move_cursor(row=keys.index(keep), animate=False)
        self._shown = keys
        self.query_one("#empty", Static).display = not rows
        t.display = bool(rows)
        if keep != self.selected_key:
            self.post_message(self.Highlighted(self, self.selected_key))

    # ---- search / sort
    def action_search(self) -> None:
        q = self.query_one("#q", Input)
        q.display = True
        q.focus()

    def action_clear_search(self) -> None:
        q = self.query_one("#q", Input)
        if q.display or self._needle:
            q.value = ""
            q.display = False
            self._needle = ""
            self._apply()
            self.table.focus()

    @on(Input.Changed, "#q")
    def _q_changed(self, e: Input.Changed) -> None:
        e.stop()
        self._needle = e.value.strip()
        self._apply()

    @on(Input.Submitted, "#q")
    def _q_submitted(self, e: Input.Submitted) -> None:
        e.stop()
        self.table.focus()

    def sort_by(self, index: int, reverse: bool | None = None) -> None:
        cur = self._sort
        rev = (not cur[1] if cur and cur[0] == index else False) if reverse is None else reverse
        self._sort = (index, rev)
        self._apply()

    def cycle_sort(self) -> None:
        i = 0 if self._sort is None else (self._sort[0] + 1) % len(self.cols)
        self._sort = (i, False)
        self._apply()
        self.app.notify(f"sorted by {self.cols[i].label}", timeout=2)

    @on(DataTable.HeaderSelected)
    def _header(self, e: DataTable.HeaderSelected) -> None:
        e.stop()
        self.sort_by(e.column_index)

    @on(DataTable.RowHighlighted)
    def _hl(self, e: DataTable.RowHighlighted) -> None:
        e.stop()
        self.post_message(self.Highlighted(self, str(e.row_key.value) if e.row_key is not None and e.row_key.value is not None else None))

    @on(DataTable.RowSelected)
    def _sel(self, e: DataTable.RowSelected) -> None:
        e.stop()
        if e.row_key is not None and e.row_key.value is not None:
            self.post_message(self.Selected(self, str(e.row_key.value)))


# ---------------------------------------------------------------------------------------------- modals
class ConfirmScreen(ModalScreen[bool]):
    """Safe confirmation. With `type_to_confirm` the button stays disabled until that word is typed."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: Any, confirm: str = "Confirm", danger: bool = False, type_to_confirm: str = "") -> None:
        super().__init__()
        self.title_, self.body, self.confirm_label, self.danger, self.word = title, body, confirm, danger, type_to_confirm

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="danger" if self.danger else ""):
            yield Label(self.title_, id="dialog-title")
            yield Static(self.body if not isinstance(self.body, str) else Text(self.body), id="dialog-body")
            if self.word:
                yield Label(f"Type  {self.word}  to confirm:", classes="hint")
                yield Input(placeholder=self.word, id="word")
            with Horizontal(classes="buttons"):
                yield Button(self.confirm_label, variant="error" if self.danger else "success", id="ok", disabled=bool(self.word))
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        (self.query_one("#word") if self.word else self.query_one("#cancel" if self.danger else "#ok")).focus()

    @on(Input.Changed, "#word")
    def _typed(self, e: Input.Changed) -> None:
        self.query_one("#ok", Button).disabled = e.value.strip() != self.word

    @on(Input.Submitted, "#word")
    def _submit(self, e: Input.Submitted) -> None:
        if e.value.strip() == self.word:
            self.dismiss(True)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"                 # text | int | select | switch
    default: Any = ""
    options: list[tuple[str, Any]] = field(default_factory=list)
    hint: str = ""
    required: bool = True
    minimum: int | None = None
    maximum: int | None = None


class FormScreen(ModalScreen["dict[str, Any] | None"]):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("ctrl+s", "submit", "Submit")]

    def __init__(self, title: str, fields: list[Field], submit: str = "OK", intro: str = "") -> None:
        super().__init__()
        self.title_, self.fields, self.submit_label, self.intro = title, fields, submit, intro

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_, id="dialog-title")
            if self.intro:
                yield Static(Text(self.intro), classes="hint")
            with VerticalScroll(id="form"):
                for f in self.fields:
                    yield Label(f.label, classes="field-label")
                    if f.kind == "select":
                        yield Select(f.options, value=f.default if f.default != "" else Select.NULL, allow_blank=not f.required, id=f"f-{f.key}")
                    elif f.kind == "switch":
                        yield Switch(value=bool(f.default), id=f"f-{f.key}")
                    else:
                        yield Input(value=str(f.default), type="integer" if f.kind == "int" else "text", id=f"f-{f.key}")
                    if f.hint:
                        yield Label(f.hint, classes="hint")
            yield Label("", id="form-error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button(self.submit_label, variant="success", id="ok")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query(f"#f-{self.fields[0].key}").first().focus()

    def _collect(self) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        for f in self.fields:
            w = self.query_one(f"#f-{f.key}")
            if isinstance(w, Select):
                v = None if w.value is Select.NULL else w.value
            elif isinstance(w, Switch):
                v = w.value
            else:
                v = w.value.strip()  # type: ignore[attr-defined]
                if f.kind == "int" and v:
                    v = int(v)
            if f.required and v in (None, ""):
                self.query_one("#form-error", Label).update(f"{f.label}: required")
                return None
            if f.kind == "int" and v not in (None, ""):
                if (f.minimum is not None and v < f.minimum) or (f.maximum is not None and v > f.maximum):
                    self.query_one("#form-error", Label).update(f"{f.label}: must be {f.minimum}–{f.maximum}")
                    return None
            out[f.key] = v
        return out

    @on(Button.Pressed, "#ok")
    def action_submit(self) -> None:
        if (v := self._collect()) is not None:
            self.dismiss(v)

    @on(Input.Submitted)
    def _enter(self, e: Input.Submitted) -> None:
        self.action_submit()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class SelectScreen(ModalScreen["Any | None"]):
    """Searchable pick-list: type to filter (all words must match), ↑/↓ to move, Enter to choose."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("down", "move(1)", show=False), Binding("up", "move(-1)", show=False)]

    def __init__(self, title: str, items: Sequence[tuple[str, Any, str]], hint: str = "Type to filter…") -> None:
        super().__init__()
        self.title_, self.items, self.hint = title, list(items), hint
        self._current: list[Any] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide"):
            yield Label(self.title_, id="dialog-title")
            yield Input(placeholder=self.hint, id="q")
            yield OptionList(id="opts")
            yield Label("Enter choose · Esc cancel", classes="hint")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one("#q", Input).focus()

    def _fill(self, needle: str) -> None:
        toks = needle.lower().split()
        matches = [it for it in self.items if all(t in f"{it[0]} {it[2]}".lower() for t in toks)]
        ol = self.query_one("#opts", OptionList)
        ol.clear_options()
        ol.add_options([Option(Text.assemble((label, "bold"), ("  " + detail, "dim"))) for label, _, detail in matches])
        self._current = [value for _, value, _ in matches]
        if matches:
            ol.highlighted = 0

    @on(Input.Changed, "#q")
    def _changed(self, e: Input.Changed) -> None:
        self._fill(e.value)

    def action_move(self, d: int) -> None:
        ol = self.query_one("#opts", OptionList)
        if ol.option_count:
            ol.highlighted = ((ol.highlighted or 0) + d) % ol.option_count

    @on(Input.Submitted, "#q")
    def _submit(self, e: Input.Submitted) -> None:
        ol = self.query_one("#opts", OptionList)
        if ol.highlighted is not None and ol.option_count:
            self.dismiss(self._current[ol.highlighted])

    @on(OptionList.OptionSelected)
    def _picked(self, e: OptionList.OptionSelected) -> None:
        self.dismiss(self._current[e.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class TaskScreen(ModalScreen[None]):
    """Live log of a long operation (deploy, scale, checkpoint…).  Esc hides it; the work continues."""

    BINDINGS = [Binding("escape", "hide", "Hide")]

    def __init__(self, title: str) -> None:
        super().__init__()
        self.title_ = title
        self.done = False
        self.stop_event = threading.Event()
        self._lines: list[str] = []
        self._result: tuple[bool, str] | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label(self.title_, id="dialog-title")
            yield RichLog(id="tlog", wrap=True, markup=False, highlight=False, max_lines=2000)
            yield Label("Working… Esc hides this window; the operation keeps running.", id="task-status", classes="hint")
            with Horizontal(classes="buttons"):
                yield Button("Hide", id="hide")

    def on_mount(self) -> None:
        for line in self._lines:      # output and result that arrived before the window finished mounting
            self.query_one("#tlog", RichLog).write(Text(line))
        if self._result:
            self._show_result(*self._result)

    def append(self, line: str) -> None:
        self._lines.append(line)
        if self.is_mounted:
            self.query_one("#tlog", RichLog).write(Text(line))

    def finish(self, ok: bool, message: str) -> None:
        self.done = True
        self._result = (ok, message)
        if self.is_mounted:
            self._show_result(ok, message)

    def _show_result(self, ok: bool, message: str) -> None:
        st = self.query_one("#task-status", Label)
        st.update(("✓ " if ok else "✗ ") + message)
        st.set_class(not ok, "error")
        st.set_class(ok, "ok")
        self.query_one("#hide", Button).label = "Close"
        self.query_one("#hide", Button).focus()

    @on(Button.Pressed, "#hide")
    def action_hide(self) -> None:
        self.dismiss(None)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", show=False)]

    def __init__(self, title: str, body: Any) -> None:
        super().__init__()
        self.title_, self.body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="wide tall"):
            yield Label(self.title_, id="dialog-title")
            with VerticalScroll():
                yield Static(self.body)
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)


class Card(Static):
    """Big-number tile: label on top, value below."""

    def __init__(self, label: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.label = label
        self.set("—")

    def set(self, value: str, sub: str = "", style: str = "") -> None:
        t = Text()
        t.append(self.label + "\n", style="dim")
        t.append(value, style=f"bold {style}".strip())
        if sub:
            t.append("\n" + sub, style="dim")
        self.update(t)


class Pane(Vertical):
    """Content of one tab.  The app calls refresh_view(kind) on the UI thread whenever its data changed."""

    primary = ""   # CSS selector of the widget that should take focus when the tab opens

    def on_mount(self) -> None:
        self.app.panes.append(self)  # type: ignore[attr-defined]
        self.refresh_view("all")

    def refresh_view(self, kind: str) -> None:
        pass

    def focus_primary(self) -> None:
        if self.primary:
            w = self.query_one(self.primary)
            w.focus_table() if isinstance(w, FilterTable) else w.focus()  # type: ignore[func-returns-value]


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(text: str) -> str:
    """Strip terminal escape codes from remote output before it goes into a log pane."""
    return _ANSI.sub("", text).replace("\r", "")
