"""The Textual client: categories | message list | reader, with arrow-key navigation."""

import os
import re
import subprocess
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import formatdate, getaddresses, make_msgid
from urllib.parse import parse_qs, unquote, urlparse

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.message import Message
from rich.markup import escape
from textual.widgets import (Collapsible, DataTable, Footer, Input, Label, OptionList, Select,
                             Static, TextArea)
from textual.widgets._collapsible import CollapsibleTitle
from textual.widgets.option_list import Option

from . import config, drafts, jev, ops, smtp, text
from .config import CACHE_DIR, Category, Config
from .imap import Folder, Msg, parse
from .store import Store

PENDING_GRACE = 30  # seconds a local change still wins over what a sync reports

KEY_HELP = [
    ("↑ ↓  PgUp PgDn  Home End", "move through the list"),
    ("← →", "previous / next tab (in the reader: back to the list)"),
    ("{goto}", "go to any folder: Starred, Sent, Bin, All Mail …"),
    ("Enter / Esc", "open conversation / go back"),
    ("↑ ↓  ·  Enter / Space", "in the reader: previous / next message  ·  expand / collapse it"),
    ("Tab / Shift+Tab  ·  Enter", "in the reader: next / previous link  ·  open it"),
    ("Space  ·  {select_all}", "select for bulk actions  ·  select all"),
    ("{move}", "move to category"),
    ("{jev}", "ask Jev where it belongs"),
    ("{archive}", "archive"),
    ("{trash}", "move to Bin (on a category: remove it)"),
    ("{undo}", "undo last action"),
    ("{toggle_read}", "mark read / unread"),
    ("{star}", "star / unstar"),
    ("{search}", "search, Gmail syntax: from:jan has:attachment"),
    ("{new}  ·  {reply}  ·  {reply_all}  ·  {forward}", "new · reply · reply all · forward"),
    ("{send}  ·  Esc  ·  {discard}", "compose: send · minimize to a draft · discard"),
    ("{sender}  ·  ↑↓ Enter", "compose: choose the From address · pick a suggested To/Cc address"),
    ("{drafts}", "continue a draft (saved automatically, also in Gmail's Drafts)"),
    ("{add_category}  ·  {rename_category}", "add category · rename the current one"),
    ("{refresh}", "refresh"),
    ("{palette}", "command palette (every action, incl. Jev history)"),
    ("{help}  ·  Ctrl+Q", "this help  ·  quit"),
]


def formataddr(pair) -> str:
    """Human-readable "Name <addr>" (email.utils.formataddr would RFC 2047-encode the name)."""
    name, addr = pair
    if not name or name == addr:
        return addr
    if any(ch in name for ch in ',;:<>@"()[]\\'):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"


def pretty(key: str) -> str:
    return "+".join(p.capitalize() if len(p) > 1 else p.upper() for p in key.split("+"))


def short_date(d: datetime | None) -> str:
    if not d:
        return ""
    d = d.astimezone()
    now = datetime.now().astimezone()
    if d.date() == now.date():
        return d.strftime("%H:%M")
    if d.year == now.year:
        return d.strftime("%-d %b")
    return d.strftime("%-d.%-m.%y")


# ------------------------------------------------------------ modals

class Picker(ModalScreen[str | None]):
    """Type to filter, arrows to choose, Enter to accept."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]], note: str = ""):
        super().__init__()
        self.title_text, self.options, self.note = title, options, note

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            if self.note:
                yield Static(self.note, classes="note")
            yield Input(placeholder="Type to filter…")
            yield OptionList(*[Option(label, id=value) for value, label in self.options])

    def on_mount(self):
        self.query_one(OptionList).highlighted = 0 if self.options else None

    @on(Input.Changed)
    def _filter(self, event: Input.Changed):
        ol = self.query_one(OptionList)
        ol.clear_options()
        q = event.value.lower()
        ol.add_options([Option(label, id=value) for value, label in self.options
                        if q in value.lower() or q in str(label).lower()])
        ol.highlighted = 0 if ol.option_count else None

    def on_key(self, event):
        ol = self.query_one(OptionList)
        if event.key in ("up", "down", "pageup", "pagedown"):
            {"up": ol.action_cursor_up, "down": ol.action_cursor_down,
             "pageup": ol.action_page_up, "pagedown": ol.action_page_down}[event.key]()
            event.stop()

    @on(Input.Submitted)
    def _submit(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            self.dismiss(ol.get_option_at_index(ol.highlighted).id)

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


class Ask(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def __init__(self, title: str, value: str = "", placeholder: str = ""):
        super().__init__()
        self.title_text, self.value, self.placeholder = title, value, placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(value=self.value, placeholder=self.placeholder)

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted):
        self.dismiss(event.value.strip() or None)


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape,n", "dismiss(False)", "No"), Binding("enter,y", "dismiss(True)", "Yes")]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="dialog-title")
            yield Static("Enter = yes    Esc = no", classes="note")


class Help(ModalScreen):
    BINDINGS = [Binding("escape,f1,enter", "dismiss", "Close")]

    def __init__(self, keys: dict[str, str]):
        super().__init__()
        self.keys = {k: pretty(v) for k, v in keys.items()}

    def compose(self) -> ComposeResult:
        t = Text()
        for key, what in KEY_HELP:
            t.append(f"{key.format(**self.keys):<34}", style="bold")
            t.append(what + "\n")
        with Vertical(classes="dialog wide"):
            yield Label("Shortcuts", classes="dialog-title")
            yield Static(t)
            yield Static("Change any of them under [keys] in ~/.config/petrzpav-mail/config.toml",
                         classes="note", markup=False)


def quote_context(quote: str) -> tuple[str, str]:
    """A reply's quote as (who wrote it when, just what they said), to read while writing."""
    head, _, rest = quote.partition("\n")
    if not head.rstrip().endswith(":"):
        head, rest = "", quote
    said = "\n".join(l[2:] if l.startswith("> ") else l[1:] if l.startswith(">") else l
                     for l in rest.splitlines())
    said = text.reflow(text.split_quoted(said)[0].strip())
    head = head.rstrip().rstrip(":")
    if m := re.fullmatch(r"On (.+?), (.+?)(?: <[^>]*>)? wrote", head):   # the line _reply writes
        head = f"{m[2]}  ·  {m[1]}"
    return head or "Earlier message", said


def fold(s: str) -> str:
    """Lowercase without diacritics, so "zelez" finds "Želez"."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


class AddressInput(Input):
    """A To/Cc field that suggests addresses from the mail seen so far, for the part after the
    last comma. ↑↓ pick a suggestion, Enter or Tab takes it, Esc closes the list."""

    def __init__(self, contacts, **kw):
        super().__init__(**kw)
        self.contacts = contacts            # callable -> [(name, addr)]
        self.list: OptionList | None = None
        self._matches: list[tuple[str, str]] = []

    def suggest(self):
        head, sep, token = self.value.rpartition(",")
        token = fold(token.strip())
        if not token or self.cursor_position < len(self.value):
            return self.close_list()
        taken = {a.lower() for _, a in getaddresses([head])}
        words = token.split()
        self._matches = [(n, a) for n, a in self.contacts() if a not in taken
                         and all(w in fold(f"{n} {a}") for w in words)][:6]
        if not self._matches:
            return self.close_list()
        self.list.set_options([Option(formataddr((n, a)) if n else a) for n, a in self._matches])
        self.list.highlighted = 0
        self.list.display = True

    def close_list(self):
        if self.list:
            self.list.display = False

    def take(self, index: int):
        name, addr = self._matches[index]
        head = self.value.rpartition(",")[0].strip()
        self.value = (head + ", " if head else "") + formataddr((name, addr)) + ", "
        self.cursor_position = len(self.value)
        self.close_list()

    async def _on_key(self, event):
        if self.list and self.list.display:
            ol = self.list
            if event.key in ("down", "up"):
                ol.highlighted = ((ol.highlighted or 0) + (1 if event.key == "down" else -1)) % ol.option_count
            elif event.key in ("enter", "tab"):
                self.take(ol.highlighted or 0)
            elif event.key == "escape":
                self.close_list()
            else:
                return await super()._on_key(event)
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def _on_blur(self, event):
        self.close_list()
        super()._on_blur(event)


class Compose(ModalScreen[str]):
    """Writing a message. Saved as you type (locally at once, to Gmail's Drafts every few
    seconds); Esc tucks it away as a draft, Alt+D throws it away, Ctrl+S sends.
    Dismisses with "sent", "minimized", "discarded" or "empty"."""

    LOCAL_DELAY = 1.0     # seconds of quiet before saving locally
    GMAIL_EVERY = 15.0    # at most this often to Gmail while typing

    BINDINGS = [Binding("escape", "minimize", "Minimize")]

    def __init__(self, app_cfg: Config, keys: dict, draft: drafts.Draft, store: Store):
        super().__init__()
        self.cfg, self.keys, self.draft, self.store = app_cfg, keys, draft, store
        self._local_timer = self._gmail_timer = None
        self._last_gmail = 0.0
        self._ready = False
        self._bindings.bind(keys["send"], "send", description="Send", priority=True)
        self._bindings.bind(keys["discard"], "discard", description="Discard", priority=True)
        self._bindings.bind(keys["sender"], "sender", description="From", priority=True)

    def compose(self) -> ComposeResult:
        d = self.draft
        # Identities with send_as can't be sent from; they reply as their target instead.
        idents = [(formataddr((i.name, i.email)), i.email) for i in self.cfg.identities if not i.send_as] or \
                 [(self.cfg.email, self.cfg.email)]
        if d.ident not in [v for _, v in idents]:
            idents.append((d.ident, d.ident))
        reply = bool(d.headers.get("In-Reply-To"))
        with Vertical(classes="compose"):
            yield Label(d.subject if reply and d.subject else "New message", classes="dialog-title",
                        markup=False)
            for fid, label, value in (("to", "To", d.to), ("cc", "Cc", d.cc), ("subject", "Subject", d.subject)):
                with Horizontal(classes="row") as row:
                    row.display = not (reply and fid == "subject")    # the title says it
                    yield Label(label, classes="field")
                    if fid == "subject":
                        yield Input(value=value, id=fid, compact=True)
                    else:
                        yield AddressInput(self.store.contacts, value=value, id=fid, compact=True)
                if fid != "subject":
                    yield OptionList(id=f"{fid}-suggest", classes="suggest")
            # After the recipients, so Tab from To/Cc/Subject reaches it before the body.
            with Horizontal(classes="row", id="from-row") as row:
                row.display = len(idents) > 1
                yield Label("From", classes="field")
                yield Select(idents, value=d.ident, allow_blank=False, id="from", compact=True)
            if d.attachments:
                yield Static("📎 " + ", ".join(a["filename"] for a in d.attachments), classes="note")
            yield TextArea(d.body, id="body", soft_wrap=True, show_line_numbers=False, compact=True)
            if d.quote:
                head, said = quote_context(d.quote)
                yield Static("↩ " + head, classes="context-head", markup=False)
                with VerticalScroll(id="context", can_focus=False):
                    yield Static(said, markup=False)
            yield Static(self._hint(), id="compose-hint", classes="note")

    def _hint(self, status: str = "") -> str:
        k = {n: pretty(v) for n, v in self.keys.items()}
        return (f"{k['send']} send  ·  Esc minimize  ·  {k['discard']} discard  ·  {k['sender']} from"
                "  ·  Tab next field"
                + (f"  ·  {status}" if status else ""))

    def on_mount(self):
        for fid in ("to", "cc"):
            field = self.query_one(f"#{fid}", AddressInput)
            field.list = self.query_one(f"#{fid}-suggest", OptionList)
            field.list.display = False
        self.query_one("#to" if not self.draft.to else "#body").focus()
        if self.draft.to:
            self.query_one("#body", TextArea).move_cursor((0, 0))
        self.call_after_refresh(lambda: setattr(self, "_ready", True))

    # -- autosave

    def _collect(self):
        d = self.draft
        d.ident = str(self.query_one("#from", Select).value)
        d.to = self.query_one("#to", Input).value.strip()
        d.cc = self.query_one("#cc", Input).value.strip()
        d.subject = self.query_one("#subject", Input).value
        d.body = self.query_one("#body", TextArea).text

    def action_sender(self):
        sel = self.query_one("#from", Select)
        if sel.parent.display:
            sel.focus()
            sel.action_show_overlay()

    @on(OptionList.OptionSelected, ".suggest")
    def _suggestion_clicked(self, event: OptionList.OptionSelected):
        field = self.query_one("#" + event.option_list.id.removesuffix("-suggest"), AddressInput)
        field.take(event.option_index)
        field.focus()

    @on(Input.Changed, "AddressInput")
    def _suggest(self, event: Input.Changed):
        if event.input.has_focus:
            event.input.suggest()

    @on(Input.Changed)
    @on(TextArea.Changed)
    @on(Select.Changed)
    def _changed(self, _event):
        if not self._ready:
            return
        if self._local_timer:
            self._local_timer.stop()
        self._local_timer = self.set_timer(self.LOCAL_DELAY, self._save_local)

    def _save_local(self):
        self._collect()
        if self.draft.untouched:       # e.g. a reply's quote rendering: nothing typed yet
            return
        drafts.save(self.draft)
        self.query_one("#compose-hint", Static).update(self._hint(f"saved {datetime.now():%H:%M}"))
        wait = self.GMAIL_EVERY - (time.time() - self._last_gmail)
        if self._gmail_timer:
            self._gmail_timer.stop()
            self._gmail_timer = None
        if wait <= 0:
            self._save_gmail()           # a zero-delay Textual timer never fires
        else:
            self._gmail_timer = self.set_timer(wait, self._save_gmail)

    def _save_gmail(self):
        self._last_gmail = time.time()
        d = self.draft
        self.store.submit("drafts", lambda: drafts.sync_to_gmail(self.cfg, self.store.drafts_mbox, d),
                          latest=f"draft:{d.id}")

    def _stop_timers(self):
        for t in (self._local_timer, self._gmail_timer):
            if t:
                t.stop()

    # -- leaving

    def action_minimize(self):
        self._stop_timers()
        self._collect()
        if self.draft.untouched:
            drafts.delete(self.draft)
            self.dismiss("empty")
            return
        drafts.save(self.draft)
        self._save_gmail()
        self.dismiss("minimized")

    def action_discard(self):
        def yes(ok):
            if not ok:
                return
            self._stop_timers()
            d = self.draft
            drafts.delete(d)
            self.store.submit("drafts", lambda: drafts.remove_from_gmail(self.store.drafts_mbox, d),
                              latest=f"draft:{d.id}")
            self.dismiss("discarded")
        if self.draft.untouched:
            yes(True)
        else:
            self.app.push_screen(Confirm("Discard this draft?"), yes)

    def action_send(self):
        self._collect()
        d = self.draft
        if not d.to:
            self.notify("Add a recipient", severity="warning")
            return
        self._stop_timers()
        d.edited = True
        drafts.save(d)                 # kept until the send succeeds
        app, cfg, store = self.app, self.cfg, self.store
        ident = cfg.identity_for(d.ident)
        app.notify(f"Sending from {ident.email}…", timeout=2)

        def send():
            smtp.send(cfg, ident, drafts.build(cfg, d))
            drafts.delete(d)
            drafts.remove_from_gmail(store.drafts_mbox, d)
            return ident.email

        def sent(address):
            app.notify(f"Sent from {address}", timeout=3)
            app.main.paint_drafts()

        def failed(e):
            app.notify(f"Not sent, kept as a draft: {e}", severity="error", timeout=10)
            app.main.paint_drafts()
        store.submit("drafts", send, sent, failed)
        self.dismiss("sending")


# ------------------------------------------------------------ main screen

class TabBar(Static):
    """One row of tabs: Inbox and the categories, with unread counts. Clickable."""

    class Picked(Message):
        def __init__(self, index: int):
            super().__init__()
            self.index = index

    def __init__(self, **kw):
        super().__init__("", **kw)
        self.items: list[tuple[str, int]] = []
        self.current = 0
        self.hint = ""
        self.spans: list[tuple[int, int, int]] = []

    def set_tabs(self, items: list[tuple[str, int]], current: int, hint: str = ""):
        self.items, self.current, self.hint = items, current, hint
        self.repaint()

    def on_resize(self):
        self.repaint()

    def repaint(self):
        width = self.size.width or 200
        pieces = [f" {name} " + (f"{n} " if n else "") for name, n in self.items]
        hint = f"  {self.hint}" if self.hint else ""
        if sum(len(p) + 1 for p in pieces) + len(hint) > width:
            hint = ""                    # tabs need the room more than the hint does
        start = 0

        def used(a, b):
            return sum(len(p) + 1 for p in pieces[a:b + 1]) + (2 if a > 0 else 0)
        while start < self.current and used(start, self.current) > width - len(hint):
            start += 1
        t, x, self.spans = Text(), 0, []
        if start > 0:
            t.append("‹ ", style="dim")
            x = 2
        for i in range(start, len(pieces)):
            name, n = self.items[i]
            piece = pieces[i]
            if x + len(piece) > width - len(hint) and i > self.current:
                t.append("›", style="dim")
                break
            cur = i == self.current
            on = "bold black on blue"
            t.append(f" {name} ", style=on if cur else ("bold" if n else ""))
            if n:
                t.append(f"{n} ", style=on if cur else "bold blue")
            self.spans.append((x, x + len(piece), i))
            x += len(piece)
            t.append("│", style="dim")
            x += 1
        if hint and x + len(hint) <= width:
            t.append(" " * (width - x - len(hint)) + hint, style="dim")
        self.update(t)

    def on_click(self, event):
        for x0, x1, i in self.spans:
            if x0 <= event.x < x1:
                self.post_message(self.Picked(i))


class Conv:
    """A Gmail conversation shown as one row. Mirrors the Msg fields the row code reads."""

    def __init__(self, msgs: list[Msg], me: set[str]):
        self.msgs = sorted(msgs, key=lambda m: (m.date.timestamp() if m.date else 0, m.uid))
        self.me = me
        first, last = self.msgs[0], self.msgs[-1]
        self.msgid = first.thrid or first.msgid          # the row key
        self.thrid = first.thrid
        self.subject = first.subject
        self.date = last.date
        self.flags = set().union(*(m.flags for m in self.msgs))
        self.labels = set().union(*(m.labels for m in self.msgs))
        self.recipients = sorted(set().union(*(m.recipients for m in self.msgs)))

    @property
    def seen(self) -> bool:
        return all(m.seen for m in self.msgs)

    @property
    def starred(self) -> bool:
        return any("\\Flagged" in m.flags for m in self.msgs)

    @property
    def unread(self) -> list[Msg]:
        return [m for m in self.msgs if not m.seen]

    @property
    def sender(self) -> str:
        names = []
        for m in self.msgs:
            if m.sender_addr.lower() in self.me:
                n = "me"
            elif m.sender and m.sender != m.sender_addr:
                n = m.sender.split()[0]
            else:
                n = m.sender_addr.split("@")[0]
            if n not in names:
                names.append(n)
        if len(names) > 1 or names == ["me"]:
            who = ", ".join(names)
        else:
            who = self.msgs[0].sender or self.msgs[0].sender_addr
        return f"{who} ({len(self.msgs)})" if len(self.msgs) > 1 else who


def group(msgs: list[Msg], me: set[str]) -> list[Conv]:
    by: dict[int, list[Msg]] = {}
    for m in msgs:
        by.setdefault(m.thrid or m.msgid, []).append(m)
    convs = [Conv(ms, me) for ms in by.values()]
    return sorted(convs, key=lambda c: c.date.timestamp() if c.date else 0, reverse=True)


class Messages(DataTable):
    pass


class Reader(VerticalScroll):
    """Keys move instantly: Textual's scroll animation looks jerky in a terminal."""

    BINDINGS = [Binding("up", "screen.message(-1)", show=False), Binding("down", "screen.message(1)", show=False),
                Binding("pagedown", "jump('page_down')", show=False), Binding("pageup", "jump('page_up')", show=False),
                Binding("home", "jump('home')", show=False), Binding("end", "jump('end')", show=False),
                Binding("tab", "screen.link(1)", show=False), Binding("shift+tab", "screen.link(-1)", show=False)]

    def action_jump(self, where: str):
        {"page_down": self.scroll_page_down, "page_up": self.scroll_page_up,
         "home": self.scroll_home, "end": self.scroll_end}[where](animate=False)


class MainScreen(Screen):
    def __init__(self, cfg: Config, store: Store):
        super().__init__()
        self.cfg, self.store = cfg, store
        self.mbox = store.fg      # user actions go through the foreground connection
        self.keys = cfg.keys
        self.views: list[Folder] = []     # every folder, for Ctrl+G
        self.tab_views: list[Folder] = []  # Inbox + categories
        self.view: Folder | None = None
        self.search_query: str | None = None
        self.raw: list[Msg] = []          # messages of the view, as Gmail lists them
        self.msgs: list[Conv] = []        # ...grouped into conversations (the rows)
        self.selected: set[int] = set()   # row keys
        self.current: Conv | None = None  # conversation shown in the reader
        self.thread_msgs: list[Msg] = []  # every message of it, incl. sent replies
        self.bodies: dict[int, EmailMessage] = {}
        self.me = {i.email.lower() for i in cfg.identities} | {cfg.email.lower()}
        self.undo_stack: list[dict] = []
        self.counts: dict[str, int] = {}
        self._prefetch_timer = None
        # Local changes Gmail may not reflect yet: msgid -> {"hide", "seen", "done"}.
        # A sync that started before the change reached Gmail must not undo it on screen.
        self.pending: dict[int, dict] = {}
        self.draft_refs: set[str] = set()  # Message-IDs that a local draft replies to
        self._link: tuple[Static, int] | None = None  # the link Tab walked to in the reader

    def compose(self) -> ComposeResult:
        yield TabBar(id="tabs")
        with Horizontal(id="panes"):
            yield Messages(id="list", cursor_type="row", zebra_stripes=False)
            with Reader(id="reader"):
                with Vertical(id="page"):
                    yield Static("", id="headers")
                    yield Vertical(id="thread")
        yield Static("", id="readhint")
        yield Static("", id="draftbar")
        yield Footer()

    def on_mount(self):
        k = self.keys
        for key, action, desc, show in [
            ("left", "pane(-1)", "", False), ("right", "pane(1)", "", False),
            ("escape", "back", "Back", False), ("enter", "open_link", "", False),
            (k["goto"], "goto", "Go to", True),
            (k["drafts"], "drafts", "Drafts", False),
            (k["select"], "select", "Select", False), (k["select_all"], "select_all", "Select all", False),
            (k["move"], "move", "Move", True), (k["jev"], "jev", "Jev", True),
            (k["archive"], "archive", "Archive", True), (k["trash"], "trash", "Delete", True),
            (k["undo"], "undo", "Undo", True), (k["toggle_read"], "toggle_read", "Read/unread", False),
            (k["star"], "star", "Star", False),
            (k["search"], "search", "Search", True), (k["new"], "new", "New", True),
            (k["reply"], "reply", "Reply", True), (k["reply_all"], "reply_all", "Reply all", False),
            (k["forward"], "forward", "Forward", False),
            (k["add_category"], "add_category", "Add category", False),
            (k["rename_category"], "rename_category", "Rename", False),
            (k["refresh"], "refresh", "Refresh", False), (k["help"], "help", "Help", True),
        ]:
            self._bindings.bind(key, action, desc, show=show,
                                priority=key in ("left", "right", "space", "escape", "delete", "enter"))
        self.refresh_bindings()
        t = self.query_one(Messages)
        # Date and subject lead; who it's from and its label follow. No header row, no chrome.
        t.show_header = False
        t.add_column("", key="mark", width=1)
        t.add_column("", key="date", width=7)
        t.add_column("", key="subject")
        t.add_column("", key="from", width=20)
        t.add_column("", key="cat", width=9)
        self.show_reader(None, None)
        # Paint what we had last time right away, then catch up with Gmail.
        folders, counts = self.store.cached_folders()
        if folders:
            self._show_folders((folders, counts))
        self.store.submit("fg", lambda: self.store.fg.folders())   # log in the action lane early
        self.load_folders()
        self.set_interval(60, self.poll)
        self.apply_layout()
        self.paint_drafts()

    # -- helpers

    def fail(self, prefix=""):
        def report(e):
            self.notify(f"{prefix}{e}", severity="error", timeout=8)
        return report

    def bg(self, fn, done=None, err_prefix=""):
        """Background lane (sync, counts, prefetch)."""
        self.store.submit("bg", fn, done, self.fail(err_prefix))

    def fg(self, fn, done=None, err_prefix="", on_error=None):
        """Action lane: what the user just asked for, in order, on its own connection."""
        def error(e):
            self.fail(err_prefix)(e)
            if on_error:
                on_error()
        self.store.submit("fg", fn, done, error)

    def focused_pane(self) -> str:
        f = self.focused
        while f is not None and f.id not in ("list", "reader"):
            f = f.parent
        return f.id if f else "list"

    def targets(self) -> list[Conv]:
        if self.selected:
            return [m for m in self.msgs if m.msgid in self.selected]
        if self.focused_pane() == "reader" and self.current:
            return [self.current]
        m = self.cursor_msg()
        return [m] if m else []

    def cursor_msg(self) -> Conv | None:
        t = self.query_one(Messages)
        if self.msgs and t.cursor_row is not None and 0 <= t.cursor_row < len(self.msgs):
            return self.msgs[t.cursor_row]
        return None

    def who(self, m: Msg) -> str:
        if m.sender_addr.lower() in self.me:
            return "me"
        if m.sender and m.sender != m.sender_addr:
            return m.sender
        return m.sender_addr.split("@")[0]

    def members(self, convs: list[Conv]) -> list[Msg]:
        return [m for c in convs for m in c.msgs]

    def badge(self, m) -> str:
        for ident in self.cfg.identities:
            if ident.email.lower() in m.recipients:
                return ident.badge
        return ""

    # -- layout: three panes when wide, fewer as the window narrows

    def on_resize(self):
        self.apply_layout()
        self.paint_drafts()
        self.call_after_refresh(self.fit_columns)

    def apply_layout(self):
        """Wide: list and reader side by side. Otherwise one of them fills the window."""
        self.mode = "wide" if self.size.width >= 140 else "narrow"
        self.set_class(self.mode == "wide", "wide")
        self.set_class(self.mode == "narrow", "narrow")
        self._show_panes()

    def _show_panes(self):
        """The list, or the reader in focus mode (full window, one centered column)."""
        reading = (self.focused_pane() if self.focused else "list") == "reader"
        self.set_class(reading, "reading")
        self.query_one(Messages).display = not reading
        self.query_one(Reader).display = reading
        if reading:
            self._paint_readhint()
        self.call_after_refresh(self.fit_columns)

    def _paint_readhint(self):
        hint = self.query_one("#readhint", Static)
        if self._link:
            links = self._links()
            i = next((n for n, l in enumerate(links) if l[:2] == self._link), 0)
            t = Text(f"{i + 1}/{len(links)}  ", style="dim")
            t.append(links[i][2] if links else "", style=text.LINK_STYLE)
            t.append("   Enter open   Tab next   Esc done", style="dim")
            t.no_wrap, t.overflow = True, "ellipsis"
            hint.update(t)
            return
        k = {n: pretty(v) for n, v in self.keys.items()}
        hint.update(f"Esc back   ↑↓ messages   Enter open   Tab links   {k['reply']} reply   "
                    f"{k['archive']} archive   {k['star']} star   {k['move']} move   Del delete")

    def on_descendant_focus(self, event):
        self._show_panes()

    def focus_pane(self, pane: str):
        w = self.query_one(f"#{pane}")
        w.display = True
        w.focus()

    # -- folders

    def load_folders(self):
        def fetch():
            folders = self.store.bg.folders(refresh=True)
            counts = self.store.bg.unseen_all()
            self.store.save_folders(folders, counts)
            return folders, counts
        self.bg(fetch, self._show_folders, "Cannot reach Gmail: ")

    def _show_folders(self, res):
        folders, self.counts = res
        by_role = {f.role: f for f in folders if f.role}
        labels = {f.name: f for f in folders if not f.role}
        views = [by_role["inbox"]] if "inbox" in by_role else []
        views += [labels[c.name] for c in self.cfg.categories if c.name in labels]
        views += [by_role[r] for r in ("starred", "sent", "drafts", "all", "spam", "trash") if r in by_role]
        self.views = views
        self.tab_views = [v for v in views if v.role == "inbox" or not v.role]
        if self.view is None and views:
            self.open_view(views[0])
            self.query_one(Messages).focus()
        self._paint_tabs()

    def _count(self, f: Folder) -> int:
        return self.counts.get(f.raw, 0) if f.role in ("", "inbox") else 0

    def _tab_list(self) -> list[Folder]:
        tabs = list(self.tab_views)
        if self.view and all(t.raw != self.view.raw for t in tabs):
            tabs.append(self.view)       # Sent, Bin, … opened via Ctrl+G
        return tabs

    def _paint_tabs(self):
        tabs = self._tab_list()
        cur = next((i for i, t in enumerate(tabs) if self.view and t.raw == self.view.raw), 0)
        items = [(t.name, self._count(t)) for t in tabs]
        if self.search_query:
            items.append((f"🔍 {self.search_query}", 0))
            cur = len(items) - 1
        self.query_one(TabBar).set_tabs(items, cur, f"←→ tabs  {pretty(self.keys['goto'])} more")

    def _paint_counts(self):
        self._paint_tabs()

    def refresh_counts(self):
        def fetch():
            counts = self.store.bg.unseen_all()
            folders = self.store.bg.folders()
            self.store.save_folders(folders, counts)
            return counts

        def done(counts):
            self.counts = counts
            self._paint_counts()
        self.store.submit("bg", fetch, done, latest="counts")

    def bump_count(self, raw: str, delta: int):
        if raw in self.counts:
            self.counts[raw] = max(0, self.counts[raw] + delta)
            self._paint_counts()

    @on(TabBar.Picked)
    def _tab_clicked(self, event: TabBar.Picked):
        tabs = self._tab_list()
        if event.index < len(tabs):
            self.open_view(tabs[event.index])
            self.focus_pane("list")

    def action_tab(self, step: int):
        tabs = self._tab_list()
        if self.search_query:
            if step < 0:
                self.open_view(self.view)
            return
        cur = next((i for i, t in enumerate(tabs) if self.view and t.raw == self.view.raw), 0)
        nxt = cur + step
        if 0 <= nxt < len(tabs):
            self.open_view(tabs[nxt])

    def action_goto(self):
        opts = []
        for f in self.views:
            n = self._count(f)
            opts.append((f.raw, Text.assemble((f"{f.name:<20}", "bold" if n else ""), (str(n or ""), "bold"))))

        def picked(raw):
            f = next((f for f in self.views if f.raw == raw), None)
            if f:
                self.open_view(f)
                self.focus_pane("list")
        self.app.push_screen(Picker("Go to", opts), picked)

    # -- message list

    def open_view(self, view: Folder, keep_cursor=False):
        self.view, self.search_query = view, None
        self.selected.clear()
        self._paint_tabs()
        cached = self.store.cached_list(view.raw)
        self.show_messages(cached or [], keep_cursor and cached is not None)
        self.load_messages(keep_cursor=True)

    def load_messages(self, keep_cursor=False):
        """Sync the current view from Gmail in the background and repaint if it changed."""
        view, query = self.view, self.search_query

        def fetch():
            started = time.time()
            msgs = self.store.bg.search(query, limit=300) if query else \
                self.store.bg.messages(view.raw, limit=300)
            return started, msgs

        def done(res):
            started, msgs = res
            msgs = self._overlay(msgs, started)
            if not query:
                self.store.save_list(view.raw, msgs)
            if (view, query) != (self.view, self.search_query):
                return  # the user moved on meanwhile
            if self._same(msgs):
                return
            self.show_messages(msgs, keep_cursor)
            if query and not msgs:
                self.notify("Nothing found")
        self.store.submit("bg", fetch, done, self.fail(), latest="view")
        self.schedule_prefetch(delay=0.1)

    def _overlay(self, msgs: list[Msg], started: float) -> list[Msg]:
        """Re-apply local changes that this sync result may predate."""
        out = []
        for m in msgs:
            p = self.pending.get(m.msgid)
            if p and p["done"] is not None and p["done"] + PENDING_GRACE < started:
                del self.pending[m.msgid]   # Gmail had it well before this sync began
                p = None
            if p and p.get("hide"):
                continue
            if p and p.get("seen") is not None:
                (m.flags.add if p["seen"] else m.flags.discard)("\\Seen")
            if p and p.get("flag") is not None:
                (m.flags.add if p["flag"] else m.flags.discard)("\\Flagged")
            out.append(m)
        return out

    def _mark_pending(self, msgs: list[Msg], **change) -> list[int]:
        ids = [m.msgid for m in msgs]
        for i in ids:
            self.pending[i] = {"hide": False, "seen": None, "flag": None, "done": None, **change}
        return ids

    def _settle(self, ids: list[int]):
        now = time.time()
        for i in ids:
            if i in self.pending:
                self.pending[i]["done"] = now

    def _same(self, msgs: list[Msg]) -> bool:
        key = lambda ms: [(m.msgid, m.thrid, m.seen, tuple(sorted(m.labels)), "\\Flagged" in m.flags) for m in ms]
        return key(msgs) == key(self.raw)

    def show_messages(self, msgs: list[Msg], keep_cursor=False):
        t = self.query_one(Messages)
        cur = self.cursor_msg() if keep_cursor else None
        row = t.cursor_row if keep_cursor else 0
        self.raw = msgs
        self.msgs = group(msgs, self.me)
        t.clear()
        for c in self.msgs:
            t.add_row(*self._cells(c), key=str(c.msgid))
        self.fit_columns()
        if self.msgs:
            if cur:
                row = next((i for i, c in enumerate(self.msgs) if c.msgid == cur.msgid), row)
            t.move_cursor(row=min(row or 0, len(self.msgs) - 1), animate=False)

    def fit_columns(self):
        """Give Subject whatever width the fixed columns leave, so nothing scrolls sideways."""
        t = self.query_one(Messages)
        cols = t.columns
        if "subject" not in cols or not t.size.width:
            return
        narrow = t.size.width < 80
        want = {"cat": 0 if narrow else 9, "from": 14 if narrow else 20}
        for k, w in want.items():
            cols[k].width = w
        fixed = sum(c.get_render_width(t) for k, c in cols.items() if k != "subject")
        want["subject"] = max(10, t.size.width - fixed - 2 * t.cell_padding - 1)
        cols["subject"].auto_width = False
        cols["subject"].width = want["subject"]
        if want != getattr(self, "_widths", None):
            self._widths = want
            t._clear_caches()          # rows rendered at the old widths would stay misaligned
            t._require_update_dimensions = True
        t.refresh()

    def _cells(self, c: Conv):
        unread = not c.seen
        if c.msgid in self.selected:
            mark = Text("●", style="bold blue")
        elif c.starred:
            mark = Text("★", style="bold yellow")
        elif unread:
            mark = Text("•", style="bold blue")
        else:
            mark = Text(" ")
        here = self.view.label if self.view and not self.search_query else None
        cats = [k.name for k in self.cfg.categories if k.name in c.labels and k.name != here]
        cat = cats[0] if cats else ""
        subject = Text(c.subject, style="bold" if unread else "", overflow="ellipsis", no_wrap=True)
        if self.has_draft(c):
            subject = Text.assemble(("✎ ", "bold"), c.subject, style=f"{'bold ' if unread else ''}yellow",
                                    overflow="ellipsis", no_wrap=True)
        return (mark,
                Text(short_date(c.date), style="bold" if unread else "dim"),
                subject,
                Text(c.sender, style="" if unread else "dim", overflow="ellipsis", no_wrap=True),
                Text(cat[:9], style=self._cat_color(cat), overflow="ellipsis", no_wrap=True))

    CAT_COLORS = ["cyan", "magenta", "green", "yellow", "blue", "red", "bright_cyan", "bright_magenta",
                  "bright_green", "bright_yellow"]

    def _cat_color(self, name: str) -> str:
        names = [k.name for k in self.cfg.categories]
        return self.CAT_COLORS[names.index(name) % len(self.CAT_COLORS)] if name in names else ""

    def redraw_row(self, c: Conv):
        t = self.query_one(Messages)
        try:
            for col, cell in zip(("mark", "date", "subject", "from", "cat"), self._cells(c)):
                t.update_cell(str(c.msgid), col, cell)
        except Exception:
            pass

    def drop_rows(self, convs: list[Conv]):
        gone = {c.msgid for c in convs}
        gone_msgs = {m.msgid for c in convs for m in c.msgs}
        t = self.query_one(Messages)
        for c in convs:
            try:
                t.remove_row(str(c.msgid))
            except Exception:
                pass
        self.msgs = [c for c in self.msgs if c.msgid not in gone]
        self.raw = [m for m in self.raw if m.msgid not in gone_msgs]
        self.selected -= gone
        if self.view and not self.search_query:
            self.store.save_list(self.view.raw, self.raw)
        if self.current and self.current.msgid in gone:
            self.show_reader(None)
            if self.focused_pane() == "reader":
                self.focus_pane("list")

    @on(DataTable.RowSelected, "#list")
    def _row_selected(self, event: DataTable.RowSelected):
        if 0 <= event.cursor_row < len(self.msgs):
            self.open_message(self.msgs[event.cursor_row])

    @on(DataTable.RowHighlighted, "#list")
    def _row_highlighted(self, event: DataTable.RowHighlighted):
        self.schedule_prefetch()

    def schedule_prefetch(self, delay=0.3):
        """Once the cursor settles, fetch bodies around it so Enter opens instantly."""
        if self._prefetch_timer:
            self._prefetch_timer.stop()
        self._prefetch_timer = self.set_timer(delay, self._prefetch)

    def _prefetch(self):
        t = self.query_one(Messages)
        row = t.cursor_row or 0
        convs = self.msgs[max(0, row - 2):row + 8]
        window = [m for c in convs for m in c.msgs[-6:]]
        thrids = [c.thrid for c in convs if c.thrid]
        if window:
            def job():
                self.store.prefetch(window)
                self.store.prefetch_threads(thrids)
            self.store.submit("bg", job, latest="prefetch")

    # -- reader: the conversation, oldest first, every message collapsed to one line

    def open_message(self, c: Conv):
        if self.view and self.view.role == "drafts" and not self.search_query:
            self.continue_gmail_draft(c.msgs[-1])
            return
        self._opening = c.msgid
        self._follow_latest = True     # until the user moves, the newest message stays open
        full = self.store.cached_thread(c.thrid) or (list(c.msgs) if not c.thrid else None)
        self.bodies = {}
        for m in (full or c.msgs):
            raw = self.store.cached_body(m)
            if raw is not None:
                self.bodies[m.msgid] = parse(raw)
        if full:
            self.show_reader(c, full)          # the complete conversation, painted once
        else:
            self.show_reader(c, None, loading=True)
        self.focus_pane("reader")

        def fetch():
            full = self.mbox.thread([c.thrid]) or list(c.msgs)
            self.store.save_thread(c.thrid, full)
            missing = [m for m in full if self.store.cached_body(m) is None]
            if missing:
                got = self.mbox.fetch_raw_many(missing[0].folder, [m.uid for m in missing])
                for m in missing:
                    if m.uid in got:
                        self.store.save_body(m, got[m.uid])
            return full

        def done(full):
            if self._opening != c.msgid:
                return
            if self.thread_msgs:
                self.update_reader(full)       # in place: only what changed
            else:
                for m in full:
                    raw = self.store.cached_body(m)
                    if raw is not None:
                        self.bodies[m.msgid] = parse(raw)
                self.show_reader(c, full)
        self.fg(fetch, done)

        unread = c.unread
        if unread:
            for m in unread:
                m.flags.add("\\Seen")
            self.redraw_row(c)
            if self.view:
                self.bump_count(self.view.raw, -len(unread))
            ids = self._mark_pending(unread, seen=True)
            self.fg(lambda: self.mbox.set_seen(unread, True),
                    lambda _: (self._settle(ids), self.refresh_counts()))

    def continue_gmail_draft(self, m: Msg):
        local = next((d for d in drafts.load_all() if d.gmail_msgid == m.msgid), None)
        if local:
            self.open_draft(local)
            return
        raw = self.store.cached_body(m)
        if raw is not None:
            self.open_draft(drafts.from_gmail(self.cfg, m, parse(raw)))
        else:
            self.fg(lambda: self.mbox.fetch(m), lambda p: self.open_draft(drafts.from_gmail(self.cfg, m, p)))

    def show_reader(self, c: Conv | None, msgs: list[Msg] | None = None, loading=False):
        """Build the conversation once, when it is opened. Later data goes through update_reader,
        which changes only what changed, so nothing jumps while you read."""
        headers = self.query_one("#headers", Static)
        box = self.query_one("#thread", Vertical)
        self.current = c
        self._gen = getattr(self, "_gen", 0) + 1
        self._link = None
        self.thread_msgs = list(msgs or [])
        box.remove_children()
        if not c:
            headers.update(Text("Enter opens the selected conversation", style="dim"))
            return
        self._paint_reader_header()
        if loading:
            box.mount(Static(Text("Loading…", style="dim")))
            return
        newest = self.thread_msgs[-1].msgid if self.thread_msgs else None
        box.mount_all([self._message_widget(m, expanded=m.msgid == newest) for m in self.thread_msgs])
        self.query_one(Reader).scroll_home(animate=False)
        if newest is not None:
            self.call_after_refresh(self._focus_message, newest, 25, True)

    def update_reader(self, full: list[Msg]):
        """Merge the full thread (sent replies, freshly downloaded bodies) into what is on screen."""
        box = self.query_one("#thread", Vertical)
        for m in full:
            if m.msgid not in self.bodies:
                raw = self.store.cached_body(m)
                if raw is not None:
                    self.bodies[m.msgid] = parse(raw)
        shown = {w.msg.msgid: w for w in self._message_widgets()}
        old_newest = self.thread_msgs[-1].msgid if self.thread_msgs else None
        new_newest = full[-1].msgid if full else None
        follow = getattr(self, "_follow_latest", False) and new_newest != old_newest
        for i, m in enumerate(full):
            w = shown.get(m.msgid)
            expand = (follow and m.msgid == new_newest) or (w is not None and not w.collapsed
                                                            and not (follow and m.msgid == old_newest))
            if w is None:                                   # a message we didn't have (e.g. my reply)
                after = next((shown[x.msgid] for x in reversed(full[:i]) if x.msgid in shown), None)
                nw = self._message_widget(m, expanded=expand)
                box.mount(nw, after=after) if after else box.mount(nw, before=0)
                shown[m.msgid] = nw
            elif w.loading and m.msgid in self.bodies:      # its body just arrived
                focused = w.has_focus_within
                nw = self._message_widget(m, expanded=expand)
                box.mount(nw, after=w)
                w.remove()
                shown[m.msgid] = nw
                if focused:
                    self.call_after_refresh(self._focus_message, m.msgid)
            elif follow and m.msgid == old_newest:
                w.collapsed = True
        self.thread_msgs = list(full)
        self._paint_reader_header()
        if follow:
            self.call_after_refresh(self._focus_message, new_newest, 25, True)

    def _paint_reader_header(self):
        c = self.current
        h = Text()
        if c.starred or any("\\Flagged" in m.flags for m in self.thread_msgs):
            h.append("★ ", style="bold yellow")
        h.append(c.subject + "\n", style="bold")
        if not self.thread_msgs:
            self.query_one("#headers", Static).update(h)
            return
        names = []
        for m in self.thread_msgs:
            if self.who(m) not in names:
                names.append(self.who(m))
        h.append(", ".join(names), style="dim")
        if len(self.thread_msgs) > 1:
            h.append(f"  ·  {len(self.thread_msgs)} messages", style="dim")
        self.query_one("#headers", Static).update(h)

    def _message_widget(self, m: Msg, expanded: bool) -> Collapsible:
        parsed = self.bodies.get(m.msgid)
        content, is_md = text.body(parsed) if parsed else ("Loading…", False)
        main, quoted = text.split_quoted(content) if parsed else (content, "")
        snippet = " ".join(main.split())[:120]
        meta = Text(style="dim")
        meta.append(f"{m.sender} <{m.sender_addr}>")
        if m.to:
            meta.append(f"\nto {m.to}")
        if m.cc:
            meta.append(f"   cc {m.cc}")
        if m.date:
            meta.append("   " + m.date.astimezone().strftime("%a %-d %b %Y, %H:%M"))
        parts = text.attachments(parsed) if parsed else []
        kids = [Static(meta, classes="meta")]
        if parts:
            kids.append(OptionList(*[Option(f"📎 {p.get_filename()}  "
                                            f"{len(p.get_payload(decode=True) or b'') // 1024} kB", id=str(i))
                                     for i, p in enumerate(parts)], classes="attachments"))
        if is_md and main:
            body = text.Links(Markdown(main))
        else:
            body = text.Links(text.linkify(text.reflow(main))) if main else "(empty)"
        kids.append(Static(body, markup=False, classes="body"))
        if quoted:
            kids.append(Collapsible(Static(text.Links(text.linkify(quoted)), markup=False, classes="history"),
                                    title="··· earlier messages", collapsed=True, classes="quoted",
                                    collapsed_symbol=" ", expanded_symbol=" "))
        dot = "•" if not m.seen else " "
        title = f"{dot} {self.who(m)}  ·  {short_date(m.date)}    {snippet}"
        col = Collapsible(*kids, title=escape(title), collapsed=not expanded,
                          collapsed_symbol="›", expanded_symbol="⌄")
        col.msg, col.gen, col.loading = m, self._gen, parsed is None
        return col

    def _message_widgets(self) -> list:
        """Message sections of the current render (old ones may still be unmounting)."""
        return [w for w in self.query_one("#thread").children
                if isinstance(w, Collapsible) and getattr(w, "gen", None) == self._gen]

    def _focus_message(self, msgid: int, tries: int = 25, reveal: bool = False):
        w = next((w for w in self._message_widgets() if w.msg.msgid == msgid), None)
        titles = w.query(CollapsibleTitle) if w else []
        if not titles:              # still mounting
            if tries:
                self.set_timer(0.02, lambda: self._focus_message(msgid, tries - 1, reveal))
            return
        title = titles.first()
        title.focus(scroll_visible=False)
        reader = self.query_one(Reader)
        if reveal:
            # Opening: bring the whole newest message into view (its top first if it's long).
            self.call_after_refresh(self._reveal, w)
        elif not reader.region.contains_region(title.region):   # moving: scroll only when out of sight
            reader.scroll_to_widget(title, animate=False, top=True)

    def _reveal(self, w, tries: int = 25):
        reader = self.query_one(Reader)
        body = w.query(".body")
        if not w.is_attached:
            return
        if (not body or body.first().size.height == 0) and tries:   # not laid out yet
            self.set_timer(0.02, lambda: self._reveal(w, tries - 1))
            return
        if w.region.bottom > reader.region.bottom or w.region.y < reader.region.y:
            reader.scroll_to_widget(w, animate=False, top=True)

    def focused_message(self) -> Msg | None:
        for w in self._message_widgets():
            if w.has_focus_within:
                return w.msg
        return None

    def action_message(self, step: int):
        """↑ ↓ in the reader: previous / next message of the conversation."""
        ws = self._message_widgets()
        if not ws:
            return
        self._follow_latest = False
        cur = next((i for i, w in enumerate(ws) if w.has_focus_within), len(ws) - 1)
        self._focus_message(ws[max(0, min(len(ws) - 1, cur + step))].msg.msgid)

    def action_toggle_message(self):
        """Space: open/close whatever is focused (a message, or its folded earlier messages)."""
        self._follow_latest = False
        w = self.focused
        while w is not None and not isinstance(w, Collapsible):
            w = w.parent
        if w is not None:
            w.collapsed = not w.collapsed
            w.scroll_visible(animate=False)

    # -- reader: links

    def _links(self) -> list[tuple[Static, int, str]]:
        """Links on screen in the reader, in reading order: (widget, index in it, href)."""
        out = []
        for w in self._message_widgets():
            for s in w.query(".body, .history"):
                if isinstance(s.content, text.Links) and not any(
                        isinstance(a, Collapsible) and a.collapsed for a in s.ancestors):
                    out += [(s, i, href) for i, (href, _) in enumerate(s.content.links)]
        return out

    def _set_link(self, link: tuple[Static, int] | None):
        old, self._link = self._link, link
        for s, i in filter(None, (old, link)):
            s.content.active = i if (s, i) == link else None
            s.update(s.content, layout=False)   # drop Textual's cached render
        self._paint_readhint()

    def action_link(self, step: int):
        """Tab / Shift+Tab in the reader: walk the links of the open messages."""
        links = self._links()
        if not links:
            self.notify("No links here", timeout=2)
            return
        keys = [l[:2] for l in links]
        if self._link in keys:
            n = (keys.index(self._link) + step) % len(links)
        else:   # start from the focused message
            m = self.focused_message()
            ws = self._message_widgets()
            owner = [next(w for w in ws if s in w.walk_children()) for s, _, _ in links]
            order = [w.msg.msgid for w in ws]
            at = order.index(m.msgid) if m and m.msgid in order else 0
            after = [n for n, w in enumerate(owner) if order.index(w.msg.msgid) >= at]
            before = [n for n, w in enumerate(owner) if order.index(w.msg.msgid) <= at]
            n = (after[0] if after else 0) if step > 0 else (before[-1] if before else len(links) - 1)
        s, i, _ = links[n]
        self._set_link((s, i))
        reader = self.query_one(Reader)
        y = s.content_region.y + s.content.links[i][1]
        if not reader.region.y <= y < reader.region.bottom - 1:
            reader.scroll_to(y=reader.scroll_y + y - reader.region.y - reader.region.height // 3,
                             animate=False)

    def action_open_link(self):
        if self.focused_pane() != "reader" or not self._link:
            raise SkipAction()
        s, i = self._link
        href = s.content.links[i][0]
        if href.startswith("mailto:"):
            u = urlparse(href)
            q = parse_qs(u.query)
            ident = self.cfg.identities[0].email if self.cfg.identities else self.cfg.email
            self.open_compose(ident=ident, to=unquote(u.path), cc=",".join(q.get("cc", [])),
                              subject=q.get("subject", [""])[0], body=q.get("body", [""])[0])
            return
        subprocess.Popen(["xdg-open", href], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self.notify(f"Opened {href[:60]}", timeout=2)

    @on(OptionList.OptionSelected, ".attachments")
    def _open_attachment(self, event: OptionList.OptionSelected):
        col = next(a for a in event.option_list.ancestors if isinstance(a, Collapsible))
        part = text.attachments(self.bodies[col.msg.msgid])[int(event.option.id)]
        folder = CACHE_DIR / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / os.path.basename(part.get_filename())
        path.write_bytes(part.get_payload(decode=True) or b"")
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        self.notify(f"Opened {path.name}")

    # -- actions: navigation

    def action_pane(self, step: int):
        """← → : switch tabs in the list; ← in the reader goes back to the list."""
        if self.focused_pane() == "reader":
            if step < 0:
                self.focus_pane("list")
            return
        self.action_tab(step)

    def action_back(self):
        if self.focused_pane() == "reader" and self._link:
            self._set_link(None)
        elif self.focused_pane() == "reader":
            self.focus_pane("list")
        elif self.selected:
            self.selected.clear()
            for m in self.msgs:
                self.redraw_row(m)
        elif self.search_query:
            self.open_view(self.view)

    def action_select(self):
        if self.focused_pane() == "reader":
            self.action_toggle_message()
            return
        if self.focused_pane() != "list":
            return
        t = self.query_one(Messages)
        m = self.cursor_msg()
        if not m:
            return
        self.selected.symmetric_difference_update({m.msgid})
        self.redraw_row(m)
        t.move_cursor(row=min(t.cursor_row + 1, len(self.msgs) - 1))

    def action_select_all(self):
        if self.focused_pane() == "reader":
            return
        self.selected = set() if len(self.selected) == len(self.msgs) else {m.msgid for m in self.msgs}
        for m in self.msgs:
            self.redraw_row(m)

    def action_refresh(self):
        self.load_folders()
        self.load_messages(keep_cursor=True)

    def poll(self):
        if self.app.screen is self:
            self.refresh_counts()
            if not self.selected:
                self.load_messages(keep_cursor=True)

    def action_help(self):
        self.app.push_screen(Help(self.keys))

    # -- actions: mail (applied on screen at once, sent to Gmail in order behind it)

    def _apply(self, convs: list[Conv], fn, remove_rows=True):
        """Act on whole conversations: shown at once, then every message of each thread
        (incl. sent replies, looked up in All Mail) is changed in Gmail behind it."""
        if not convs:
            return
        ids = []
        if remove_rows:
            members = self.members(convs)
            unread = sum(1 for m in members if not m.seen)
            ids = self._mark_pending(members, hide=True)
            self.drop_rows(convs)
            if self.view and unread:
                self.bump_count(self.view.raw, -unread)

        def job():
            full = self.mbox.thread([c.thrid for c in convs])
            known = {m.thrid for m in full}
            full += [m for c in convs if c.thrid not in known for m in c.msgs]
            return fn(full)

        def done(rec):
            self._settle(ids)
            self.undo_stack.append(rec)
            n = len(convs)
            self.notify(f"{rec['what']} ({n} conversation{'s' if n != 1 else ''})  ·  "
                        f"{pretty(self.keys['undo'])} to undo", timeout=3)
            self.refresh_counts()

        def failed():
            for i in ids:
                self.pending.pop(i, None)
            self.load_messages(keep_cursor=True)
        self.fg(job, done, on_error=failed)

    def _category_options(self, exclude: str | None = None):
        return [(c.name, c.name) for c in self.cfg.categories if c.name != exclude]

    def _view(self):
        return None if self.search_query else self.view

    def action_move(self):
        convs = self.targets()
        if not convs:
            return
        here = self.view.name if self.view and not self.view.role and not self.search_query else None

        def picked(name):
            if name:
                view = self._view()
                self._apply(convs, lambda full: ops.move(self.mbox, full, view, name),
                            remove_rows=not self.search_query)
        self.app.push_screen(Picker(f"Move {len(convs)} to…" if len(convs) > 1 else "Move to…",
                                    self._category_options(here)), picked)

    def action_jev(self):
        convs = self.targets()[:1]
        if not convs:
            return
        c = convs[0]
        m = c.msgs[-1]
        self.notify("Asking Jev…", timeout=2)

        def done(v):
            ranked = sorted(v.probabilities.items(), key=lambda kv: -kv[1])
            opts = []
            for name, p in ranked:
                if name == "Inbox" and self.view and self.view.role == "inbox":
                    continue
                bar = "█" * round(p * 20)
                opts.append((name, Text.assemble((f"{name:<16}", "bold" if name == v.choice else ""),
                                                 (f"{p * 100:5.1f}%  ", ""), (bar, "dim"))))

            def picked(name):
                if not name:
                    return
                view = self._view()
                dst = "\\Inbox" if name == "Inbox" else name
                remove = not self.search_query and not (name == "Inbox" and view and view.role == "inbox")
                self._apply(convs, lambda full: ops.move(self.mbox, full, view, dst), remove_rows=remove)
            self.app.push_screen(Picker(f"Jev: {v.choice}  ({v.confidence * 100:.0f}% confident)", opts,
                                        note=c.subject), picked)

        def classify():
            raw = self.store.cached_body(m)
            body = text.body_text(parse(raw)) if raw else ""
            if not raw and m.size < 1_500_000:
                data = self.mbox.fetch_raw(m)
                self.store.save_body(m, data)
                body = text.body_text(parse(data))
            return jev.classify(self.cfg, f"{m.sender} <{m.sender_addr}>", m.to, m.subject, body)
        self.fg(classify, done, "Jev: ")

    def action_archive(self):
        convs = self.targets()
        if self.view and self.view.role not in ("", "inbox") and not self.search_query:
            self.notify("Archive works in Inbox and categories", severity="warning")
            return
        view = self._view()
        self._apply(convs, lambda full: ops.archive(self.mbox, full, view), remove_rows=not self.search_query)

    def action_trash(self):
        convs = self.targets()
        if self.view and self.view.role == "trash":
            self.notify("Already in Bin", severity="warning")
            return
        view = self._view()
        self._apply(convs, lambda full: ops.trash(self.mbox, full, view))

    def action_undo(self):
        if not self.undo_stack:
            self.notify("Nothing to undo")
            return
        rec = self.undo_stack.pop()
        for i in rec.get("msgids") or [it["msgid"] for it in rec.get("items", [])]:
            self.pending.pop(i, None)

        def done(msg):
            self.notify(msg, timeout=3)
            self.load_messages(keep_cursor=True)
            self.refresh_counts()
        self.fg(lambda: ops.undo(self.mbox, rec), done)

    def action_star(self):
        convs = self.targets()
        if not convs:
            return
        on = not all(c.starred for c in convs)
        changed = []
        for c in convs:
            if on and not c.starred:
                c.msgs[-1].flags.add("\\Flagged")
                changed.append(c.msgs[-1])
            elif not on:
                for m in c.msgs:
                    if "\\Flagged" in m.flags:
                        m.flags.discard("\\Flagged")
                        changed.append(m)
            self.redraw_row(c)
        if self.current and self.current in convs:
            if on:
                self.thread_msgs[-1].flags.add("\\Flagged") if self.thread_msgs else None
            else:
                for m in self.thread_msgs:
                    m.flags.discard("\\Flagged")
            self._paint_reader_header()
        ids = self._mark_pending(changed, flag=on)

        def job():
            if on:
                # the message just marked on screen, so a later sync agrees with it
                return ops.star(self.mbox, [m for m in changed], True)
            full = self.mbox.thread([c.thrid for c in convs])
            known = {m.thrid for m in full}
            full += [m for c in convs if c.thrid not in known for m in c.msgs]
            return ops.star(self.mbox, full, False)

        def done(rec):
            self._settle(ids)
            self.undo_stack.append(rec)
            self.notify(f"{rec['what']}  ·  {pretty(self.keys['undo'])} to undo", timeout=2)
        self.fg(job, done, on_error=lambda: self.load_messages(keep_cursor=True))

    def action_toggle_read(self):
        convs = self.targets()
        if not convs:
            return
        seen = not all(c.seen for c in convs)
        changed = [m for m in self.members(convs) if m.seen != seen]
        for m in changed:
            (m.flags.add if seen else m.flags.discard)("\\Seen")
        for c in convs:
            self.redraw_row(c)
        if self.view:
            self.bump_count(self.view.raw, -len(changed) if seen else len(changed))
        ids = self._mark_pending(changed, seen=seen)
        self.fg(lambda: ops.set_read(self.mbox, changed, seen),
                lambda _: (self._settle(ids), self.refresh_counts()))

    def action_search(self):
        def got(q):
            if q:
                self.search_query = q
                self.selected.clear()
                self._paint_tabs()
                self.show_messages([])
                self.load_messages()
                self.query_one(Messages).focus()
        self.app.push_screen(Ask("Search all mail", self.search_query or "",
                                 "from:jan has:attachment newer_than:30d to:pavel@petrzela.eu"), got)

    def jev_history(self):
        entries = ops.sort_history(40)
        if not entries:
            self.notify("Jev hasn't moved anything yet")
            return
        opts = [(str(i), Text.assemble((datetime.fromtimestamp(e["ts"]).strftime("%-d %b %H:%M  "), "dim"),
                                       (f"{e['choice']:<14}", "bold"), f"{e['subject'][:60]}"))
                for i, e in enumerate(entries)]

        def picked(i):
            if i is None:
                return
            e = entries[int(i)]

            def done(msg):
                ops.mark_undone(e["msgid"])
                self.notify(f"Back in Inbox: {e['subject'][:50]}")
                self.load_messages(keep_cursor=True)
                self.refresh_counts()
            self.fg(lambda: ops.undo(self.mbox, e["undo"]), done)
        self.app.push_screen(Picker("Jev moved these — Enter puts one back in Inbox", opts), picked)

    # -- compose

    def open_compose(self, ident: str, attach=None, **kw):
        self.open_draft(drafts.new(ident, attach_parts=attach, **kw))

    def open_draft(self, d: drafts.Draft):
        def closed(result):
            self.paint_drafts()
            if result == "minimized":
                self.notify(f"Draft saved · {pretty(self.keys['drafts'])} to continue", timeout=3)
            if result in ("sent", "sending") and self.view and self.view.role in ("sent", "drafts"):
                self.load_messages(keep_cursor=True)
        self.app.push_screen(Compose(self.cfg, self.keys, d, self.store), closed)

    # -- drafts bar

    def has_draft(self, c: Conv) -> bool:
        return any(m.message_id and m.message_id in self.draft_refs for m in c.msgs)

    def paint_drafts(self):
        ds = drafts.load_all()
        refs = {r for d in ds for k in ("In-Reply-To", "References") for r in d.headers.get(k, "").split()}
        if refs != self.draft_refs:
            before = {c.msgid for c in self.msgs if self.has_draft(c)}
            self.draft_refs = refs
            for c in self.msgs:
                if (c.msgid in before) != self.has_draft(c):
                    self.redraw_row(c)
        bar = self.query_one("#draftbar", Static)
        bar.display = bool(ds)
        if ds:
            t = Text("✎ ", style="bold yellow")
            for i, d in enumerate(ds[:4]):
                if i:
                    t.append("   ·   ", style="dim")
                t.append(d.title[:40], style="bold" if i == 0 else "")
                if d.to:
                    t.append(f"  → {d.to[:24]}", style="dim")
            if len(ds) > 4:
                t.append(f"   +{len(ds) - 4}", style="dim")
            t.append(f"      {pretty(self.keys['drafts'])} open", style="dim")
            bar.update(t)

    def action_drafts(self):
        ds = drafts.load_all()
        if not ds:
            self.notify("No drafts")
            return
        if len(ds) == 1:
            self.open_draft(ds[0])
            return
        opts = [(d.id, Text.assemble((f"{d.title[:50]:<52}", "bold"), (d.to[:30], "dim"),
                                     (f"  {datetime.fromtimestamp(d.updated):%-d %b %H:%M}", "dim")))
                for d in ds]

        def picked(i):
            d = next((d for d in ds if d.id == i), None)
            if d:
                self.open_draft(d)
        self.app.push_screen(Picker("Drafts", opts), picked)

    def on_click(self, event):
        if getattr(event, "widget", None) is self.query_one("#draftbar"):
            self.action_drafts()

    def action_new(self):
        ident = self.cfg.identities[0].email if self.cfg.identities else self.cfg.email
        self.open_compose(ident=ident)

    def reply_target(self) -> Msg | None:
        """The focused message in the reader, else the newest one not sent by me."""
        m = self.focused_message() if self.focused_pane() == "reader" else None
        if m:
            return m
        convs = self.targets()[:1]
        if not convs:
            return None
        msgs = self.thread_msgs if self.current and self.current.msgid == convs[0].msgid else convs[0].msgs
        theirs = [x for x in msgs if x.sender_addr.lower() not in self.me]
        return (theirs or msgs)[-1]

    def _reply(self, all_=False, forward=False):
        m = self.reply_target()
        if not m:
            return

        def go(parsed):
            ident = self.cfg.identity_for(*m.recipients)
            body = text.body_text(parsed)
            when = m.date.astimezone().strftime("%a %-d %b %Y %H:%M") if m.date else ""
            subj = m.subject
            if forward:
                if not subj.lower().startswith(("fwd:", "fw:")):
                    subj = "Fwd: " + subj
                fwd = (f"\n\n---------- Forwarded message ---------\nFrom: {m.sender} <{m.sender_addr}>\n"
                       f"Date: {when}\nSubject: {m.subject}\nTo: {m.to}\n\n{body}")
                self.open_compose(ident=ident.email, subject=subj, body=fwd, attach=text.attachments(parsed))
                return
            if not subj.lower().startswith("re:"):
                subj = "Re: " + subj
            reply_to = formataddr(getaddresses([str(parsed.get("Reply-To") or "")])[0]) \
                if parsed.get("Reply-To") else formataddr((m.sender, m.sender_addr))
            cc = ""
            if all_:
                mine = {i.email.lower() for i in self.cfg.identities} | {self.cfg.email.lower()}
                others = [formataddr((n, a)) for n, a in getaddresses([m.to, m.cc])
                          if a and a.lower() not in mine and a.lower() != m.sender_addr.lower()]
                cc = ", ".join(others)
            refs = " ".join(x for x in (parsed.get("References", ""), m.message_id) if x).strip()
            headers = {"In-Reply-To": m.message_id, "References": refs} if m.message_id else {}
            quoted = f"On {when}, {m.sender} <{m.sender_addr}> wrote:\n{text.quote(body)}"
            self.open_compose(ident=ident.email, to=str(reply_to), cc=cc, subject=subj, quote=quoted,
                          headers=headers)

        if m.msgid in self.bodies:
            go(self.bodies[m.msgid])
        else:
            raw = self.store.cached_body(m)
            if raw is not None:
                go(parse(raw))
            else:
                self.fg(lambda: self.mbox.fetch(m), go)

    def action_reply(self):
        self._reply()

    def action_reply_all(self):
        self._reply(all_=True)

    def action_forward(self):
        self._reply(forward=True)

    # -- categories

    def action_add_category(self):
        def got_name(name):
            if not name:
                return
            if self.cfg.category(name):
                self.notify(f"{name} already exists", severity="warning")
                return

            def got_desc(desc):
                def create():
                    self.mbox.create_label(name)
                    self.cfg.categories.append(Category(name, desc or ""))
                    config.save_categories(self.cfg.categories)

                def done(_):
                    self.notify(f"Category {name} added (Gmail label created)")
                    self.load_folders()
                self.fg(create, done)
            self.app.push_screen(Ask(f"What belongs in {name}? (Jev reads this)", "",
                                     "e.g. Bills, receipts and payment confirmations"), got_desc)
        self.app.push_screen(Ask("New category name"), got_name)

    def _current_category(self) -> Category | None:
        if not self.view or self.view.role or self.search_query:
            return None
        return self.cfg.category(self.view.name)

    def action_rename_category(self):
        cat = self._current_category()
        if not cat:
            self.notify("Switch to a category tab first", severity="warning")
            return

        def got_name(new):
            if not new or new == cat.name:
                return

            def got_desc(desc):
                def go():
                    self.mbox.rename_label(cat.name, new)
                    cat.name, cat.description = new, desc if desc is not None else cat.description
                    config.save_categories(self.cfg.categories)

                def done(_):
                    self.notify(f"Renamed to {new}")
                    self.view = None
                    self.load_folders()
                self.fg(go, done)
            self.app.push_screen(Ask(f"Description of {new}", cat.description), got_desc)
        self.app.push_screen(Ask(f"Rename {cat.name} to", cat.name), got_name)

    def remove_category(self):
        cat = self._current_category()
        if not cat:
            self.notify("Switch to a category tab first", severity="warning")
            return

        def yes(ok):
            if not ok:
                return

            def go():
                self.cfg.categories = [c for c in self.cfg.categories if c.name != cat.name]
                config.save_categories(self.cfg.categories)
                self.mbox.delete_label(cat.name)

            def done(_):
                self.notify(f"Removed {cat.name}; its mail stays in All Mail")
                self.view = None
                self.load_folders()
            self.fg(go, done)
        self.app.push_screen(Confirm(f"Remove category {cat.name}? The Gmail label is deleted, "
                                     "the emails stay in All Mail."), yes)


# ------------------------------------------------------------ app

class MailApp(App):
    TITLE = "Mail"
    PAUSE_GC_ON_SCROLL = True
    CSS = """
    Screen { background: ansi_default; }
    #tabs { height: 1; background: ansi_default; }
    #panes { border-top: solid $panel-lighten-2; }
    #list { width: 1fr; background: ansi_default; overflow-x: hidden; }
    /* Reading is a focus mode: one centered column, nothing else on screen (omawrite-like). */
    #reader { width: 1fr; background: ansi_default; align-horizontal: center; scrollbar-size-vertical: 1; }
    MainScreen.reading #tabs, MainScreen.reading #list, MainScreen.reading Footer { display: none; }
    MainScreen.reading #panes { border-top: none; }
    #page { width: 100%; max-width: 76; height: auto; padding: 2 2 3 2; }
    #headers { padding-bottom: 2; }
    #thread { height: auto; }
    #thread Collapsible { border-top: none; padding: 0; margin: 0 0 0 0; background: ansi_default; }
    #thread CollapsibleTitle { padding: 0; width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis;
                               color: $text-muted; }
    #thread CollapsibleTitle:hover { background: ansi_default; }
    #thread CollapsibleTitle:focus { background: ansi_default; color: ansi_default; text-style: bold; }
    #thread Collapsible.-expanded { margin: 0 0 1 0; }
    #thread Collapsible > Contents { padding: 1 0 1 2; }
    #thread .meta { color: $text-muted; padding-bottom: 1; }
    #thread .attachments { height: auto; max-height: 6; border: none; margin-bottom: 1; padding: 0;
                           background: ansi_default; }
    #thread .body { padding-bottom: 1; }
    #thread .quoted CollapsibleTitle { color: $text-muted; text-style: none; }
    #thread .quoted .history { color: $text-muted; }
    #readhint { dock: bottom; height: 1; color: $text-muted; display: none; padding: 0 2; }
    #draftbar { dock: bottom; height: 1; padding: 0 1; background: ansi_default; display: none; }
    MainScreen.reading #readhint { display: block; }
    Messages > .datatable--header { background: ansi_default; text-style: bold; }
    .dialog { width: 70; max-width: 95%; height: auto; max-height: 80%; border: round $accent;
              background: $surface; padding: 1 2; }
    .dialog.wide { width: 90; }
    .dialog OptionList { height: auto; max-height: 20; border: none; }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .note { color: $text-muted; padding-top: 1; }
    ModalScreen { align: center middle; }
    .compose { width: 100; max-width: 98%; height: 90%; border: round $accent; background: $surface;
               padding: 1 2; }
    .compose .row { height: 1; }
    .compose .field { width: 9; color: $text-muted; }
    .compose Input, .compose Select { width: 1fr; background: $surface; }
    .compose Input:focus { background: $boost; }
    .compose Select.-textual-compact:focus > SelectCurrent { background: ansi_blue; color: ansi_black; }
    .compose Select.-textual-compact:focus > SelectCurrent Static#label { color: ansi_black; }
    .compose SelectOverlay { background: $panel; border: round $accent; }
    .compose .suggest { height: auto; max-height: 8; margin-left: 9; border: round $accent;
                        background: $panel; }
    .compose .suggest > .option-list--option-highlighted { background: ansi_blue; color: ansi_black; }
    .compose TextArea { height: 1fr; margin-top: 1; padding: 1 0 0 0; background: $surface;
                        border-top: hkey ansi_bright_black; }
    .compose .context-head { color: $text-muted; margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .compose #context { height: auto; max-height: 35%; color: $text-muted; padding: 0 0 0 1;
                        border-left: outer ansi_bright_black; scrollbar-size-vertical: 1; }
    /* The ANSI theme leaves selections the same colour as the background: make them visible. */
    TextArea > .text-area--selection { background: ansi_blue; color: ansi_black; text-style: none; }
    Input > .input--selection { background: ansi_blue; color: ansi_black; }
    """

    def __init__(self, cfg: Config, compose: str | None = None):
        super().__init__()
        self.cfg, self.compose_url = cfg, compose
        self.store = Store(cfg, self._call_ui)
        self.mbox = self.store.fg
        self.COMMAND_PALETTE_BINDING = cfg.keys.get("palette", "ctrl+k")

    def on_mount(self):
        self.theme = "ansi-dark"
        # Links in HTML/Markdown mail look the same as in plain text.
        from rich.theme import Theme
        self.console.push_theme(Theme({"markdown.link": text.LINK_STYLE, "markdown.link_url": text.LINK_STYLE}))
        self.main = MainScreen(self.cfg, self.store)
        self.push_screen(self.main)
        if self.compose_url is not None:
            self.call_after_refresh(self._mailto, self.compose_url)

    def _mailto(self, url: str):
        u = urlparse(url) if url.startswith("mailto:") else None
        q = parse_qs(u.query) if u else {}
        to = unquote(u.path) if u else url
        ident = self.cfg.identities[0].email if self.cfg.identities else self.cfg.email

        def closed(result):
            if result == "minimized":
                self.main.paint_drafts()     # stay open on the list, the draft in the bar
                return
            self.exit()
        d = drafts.new(ident, to=to, cc=",".join(q.get("cc", [])), subject=q.get("subject", [""])[0],
                       body=q.get("body", [""])[0])
        self.push_screen(Compose(self.cfg, self.cfg.keys, d, self.store), closed)

    def get_system_commands(self, screen):
        m = self.main
        yield SystemCommand("Jev history", "Recent automatic moves; put one back", m.jev_history)
        yield SystemCommand("Sort inbox with Jev now", "Run the auto-sort once", self.sort_now)
        for name, action, help_ in [
            ("Move to category", m.action_move, "File the message under a category"),
            ("Ask Jev", m.action_jev, "Suggest a category"),
            ("Archive", m.action_archive, "Remove from Inbox / category"),
            ("Delete", m.action_trash, "Move to Bin"),
            ("Undo", m.action_undo, "Undo the last action"),
            ("Mark read / unread", m.action_toggle_read, ""),
            ("Star / unstar", m.action_star, "Toggle the star on the conversation"),
            ("Search", m.action_search, "Gmail search syntax"),
            ("New message", m.action_new, ""),
            ("Drafts", m.action_drafts, "Continue a saved draft"),
            ("Reply", m.action_reply, ""),
            ("Reply all", m.action_reply_all, ""),
            ("Forward", m.action_forward, ""),
            ("Go to folder", m.action_goto, "Starred, Sent, Bin, All Mail …"),
            ("Add category", m.action_add_category, "Creates the Gmail label"),
            ("Remove category", m.remove_category, "Deletes the current tab's Gmail label; mail stays in All Mail"),
            ("Rename category", m.action_rename_category, ""),
            ("Refresh", m.action_refresh, ""),
            ("Keyboard shortcuts", m.action_help, ""),
        ]:
            yield SystemCommand(name, help_, action)
        yield SystemCommand("Quit", "Close the mail client", self.exit)

    def sort_now(self):
        m = self.main
        lines = []

        def done(n):
            self.notify(f"Jev moved {n} message(s)" if n else "Jev: nothing to move")
            m.load_messages(keep_cursor=True)
            m.refresh_counts()
        m.bg(lambda: ops.sort_inbox(self.cfg, self.store.bg, report=lines.append), done, "Jev: ")

    def _call_ui(self, fn, *args):
        try:
            self.call_from_thread(fn, *args)
        except Exception:  # noqa: BLE001 - app already closing
            pass

    def on_unmount(self):
        self.store.close()
        self.store.prune_bodies()
