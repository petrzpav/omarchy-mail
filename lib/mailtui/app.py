"""The Textual client: categories | message list | reader, with arrow-key navigation."""

import os
import re
import shutil
import subprocess
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import formatdate, getaddresses, make_msgid
from urllib.parse import parse_qs, unquote, urlparse

from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
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
from textual.worker import get_current_worker

from . import config, drafts, jev, ops, smtp, store as cache, text
from .config import CACHE_DIR, Category, Config
from .imap import Folder, Msg, parse
from .store import Store
from .text import formataddr

TRELLO_LINK = re.compile(r"https?://(www\.)?trello\.com/[bc]/")

PENDING_GRACE = 30  # seconds a local change still wins over what a sync reports
POLL_PUSHED = 120   # with push working, a safety sync this often
POLL_ALONE = 20     # without it (connection down), this often

KEY_HELP = [
    "Moving around",
    ("↑ ↓  PgUp PgDn  Home End", "move through the list"),
    ("← →", "previous / next tab (in the reader: back to the list)"),
    ("{palette}", "go to a folder or a conversation; type > for every command"),
    ("Enter  ·  Esc", "open conversation  ·  go back"),
    ("{search}", "search, Gmail syntax: from:jan has:attachment"),
    "Reading",
    ("↑ ↓", "scroll; past a message's end: previous / next message"),
    ("Enter / Space", "expand / collapse the message"),
    ("Tab / Shift+Tab  ·  Enter", "next / previous link  ·  open it"),
    ("Ctrl+click", "open a link"),
    ("drag  ·  Ctrl+C", "select text  ·  copy it"),
    "Sorting",
    ("{select}  ·  {select_all}", "select for bulk actions  ·  select all"),
    ("{move}", "move to category"),
    ("{jev}", "ask Jev where it belongs"),
    ("{archive}", "archive"),
    ("{trash}", "move to Bin (on a category: remove it)"),
    ("{toggle_read}", "mark read / unread"),
    ("{star}", "star / unstar"),
    ("{undo}", "undo last action"),
    "Writing",
    ("{new}", "new message"),
    ("{reply}", "reply"),
    ("{reply_all}", "reply all"),
    ("{forward}", "forward"),
    ("{drafts}", "continue a draft (saved automatically, also in Gmail's Drafts)"),
    ("{send}", "compose: send"),
    ("Esc", "compose: minimize to a draft"),
    ("{discard}", "compose: discard"),
    ("{sender}", "compose: choose the From address"),
    ("↑ ↓  Enter", "compose: pick a suggested To / Cc address"),
    ("{review}", "compose: ask Jev how good the message is (also when typing pauses)"),
    "Categories",
    ("{add_category}", "add category"),
    ("{rename_category}", "rename the current one"),
    "Other",
    ("{refresh}", "refresh"),
    ("{help}  ·  Ctrl+Q", "this help  ·  quit"),
]


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
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold", no_wrap=True)
        t.add_column()
        for i, row in enumerate(KEY_HELP):
            if isinstance(row, str):    # a section heading
                t.add_row(Text(("\n" if i else "") + row, style="bold underline"), "")
            else:
                t.add_row(row[0].format(**self.keys), row[1])
        with VerticalScroll(classes="dialog wide") as body:    # scrolls when the window is short
            yield Label("Shortcuts   ↑ ↓ scroll · Esc close", classes="dialog-title")
            yield Static(t)
            yield Static("Change any of them under [keys] in ~/.config/petrzpav-mail/config.toml",
                         classes="note", markup=False)
        self.body = body

    def on_mount(self):
        self.body.focus()


def quote_context(quote: str) -> tuple[str, str]:
    """A reply's quote or a forwarded message as (who wrote it when, just what they said)."""
    if quote.startswith(text.FORWARD_HEAD):
        fields, _, said = quote.partition("\n\n")
        f = dict(l.split(": ", 1) for l in fields.splitlines()[1:] if ": " in l)
        who = re.sub(r"\s*<[^>]*>$", "", f.get("From", "")) or "Forwarded message"
        return "  ·  ".join(x for x in (who, f.get("Date", "")) if x), text.reflow(said.strip())
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


def rank(items: list[dict], query: str, limit=80) -> list[dict]:
    """items: {"text", "detail" (optional), "boost"}. Every word must appear; word starts and
    earlier hits rank higher; with few hits, fuzzy matches fill in."""
    q = fold(query).split()
    if not q:
        return items[:limit]
    scored = []
    for it in items:
        f = fold(it["text"] + "  " + it.get("detail", ""))
        score = it.get("boost", 0)
        for w in q:
            i = f.find(w)
            if i < 0:
                break
            score += 10 - min(i, 9) * 0.5 + (5 if i == 0 or not f[i - 1].isalnum() else 0)
        else:
            scored.append((score, it))
    if len(scored) < 15:
        from textual.fuzzy import FuzzySearch
        fz, have, qq = FuzzySearch(), {id(it) for _, it in scored}, fold(query)
        for it in items:
            if id(it) not in have and (s := fz.match(qq, fold(it["text"]))[0]) > 0:
                scored.append((s / 10 + it.get("boost", 0) - 100, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def highlight(s: str, query: str, style="") -> Text:
    t, f = Text(s, style=style), fold(s)
    for w in fold(query).split():
        start = 0
        while (i := f.find(w, start)) >= 0:
            t.stylize("bold underline", i, i + len(w))
            start = i + len(w)
    return t


class Palette(ModalScreen):
    """Ctrl+P: type to search what `source(query)` returns, ↑↓ to choose, Enter to go."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close"),
                Binding("up", "move(-1)", show=False, priority=True),
                Binding("down", "move(1)", show=False, priority=True),
                Binding("pageup", "move(-10)", show=False, priority=True),
                Binding("pagedown", "move(10)", show=False, priority=True)]

    def __init__(self, title: str, source, placeholder="Type to search…"):
        super().__init__()
        self.title_text, self.source, self.placeholder = title, source, placeholder
        self.items: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(placeholder=self.placeholder)
            yield OptionList()

    def on_mount(self):
        self.refill("")

    def refill(self, query: str):
        ol = self.query_one(OptionList)
        self.items = self.source(query)
        opts = []
        for i, it in enumerate(self.items):
            t = it.get("prefix", Text()) + highlight(it["text"], query.lstrip(">"), it.get("style", ""))
            if it.get("detail"):
                t += Text("  " + it["detail"], style="dim")
            t.no_wrap, t.overflow = True, "ellipsis"
            opts.append(Option(t, id=str(i)))
        ol.set_options(opts)
        ol.highlighted = 0 if opts else None

    @on(Input.Changed)
    def _changed(self, event: Input.Changed):
        self.refill(event.value)

    def action_move(self, step: int):
        ol = self.query_one(OptionList)
        if ol.option_count:
            ol.highlighted = max(0, min(ol.option_count - 1, (ol.highlighted or 0) + step))

    @on(Input.Submitted)
    def _submit(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None and self.items:
            self.dismiss(self.items[ol.highlighted])

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected):
        self.dismiss(self.items[int(event.option.id)])


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
    The `mail` command (an AI agent) may change, send or discard the same draft meanwhile: its
    changes show up here and merge with mine, and what's on screen never overwrites them.
    Dismisses with "sent", "minimized", "discarded", "empty" or "gone"."""

    LOCAL_DELAY = 1.0     # seconds of quiet before saving locally
    GMAIL_EVERY = 15.0    # at most this often to Gmail while typing
    REVIEW_DELAY = 4.0    # seconds of quiet before Jev grades the message
    REVIEW_MIN = 20       # characters written before it's worth grading
    OUTSIDE_EVERY = 1.0   # seconds between looks for changes made outside (the `mail` command)

    BINDINGS = [Binding("escape", "minimize", "Minimize")]

    def __init__(self, app_cfg: Config, keys: dict, draft: drafts.Draft, store: Store):
        super().__init__()
        self.cfg, self.keys, self.draft, self.store = app_cfg, keys, draft, store
        self._local_timer = self._gmail_timer = self._review_timer = None
        self._reviewed = ""               # the body the shown grades are for
        self._last_gmail = 0.0
        self._ready = False
        self._base = drafts.fields(draft)   # the saved version the screen is based on
        self._merged = False                # an outside change came in (send waits for a look)
        self._closing = False               # gone while a dialog was on top: close when back
        self._lost = False                  # sent or discarded outside (send must not go on)
        self._outside_timer = None
        self._bindings.bind(keys["send"], "send", description="Send", priority=True)
        self._bindings.bind(keys["discard"], "discard", description="Discard", priority=True)
        self._bindings.bind(keys["sender"], "sender", description="From", priority=True)
        self._bindings.bind(keys["review"], "review", description="Jev review", priority=True)

    def compose(self) -> ComposeResult:
        d = self.draft
        # Identities with send_as can't be sent from; they reply as their target instead.
        idents = [(formataddr((i.name, i.email)), i.email) for i in self.cfg.identities if not i.send_as] or \
                 [(self.cfg.email, self.cfg.email)]
        if d.ident not in [v for _, v in idents]:
            idents.append((d.ident, d.ident))
        reply = bool(d.headers.get("In-Reply-To"))
        self._idents = [v for _, v in idents]
        with Vertical(classes="compose"):
            yield Label(d.subject if reply and d.subject else "Forward" if d.forward else "New message",
                        classes="dialog-title", markup=False)
            with Horizontal(id="compose-main"):
                with Vertical(id="compose-left"):
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
                        yield Static("📎 " + ", ".join(text.clean_name(a["filename"]) for a in d.attachments), classes="note")
                    yield TextArea(d.body, id="body", soft_wrap=True, show_line_numbers=False, compact=True)
                    if d.quote:
                        head, said = quote_context(d.quote)
                        yield Static(("↪ " if d.forward else "↩ ") + head, classes="context-head", markup=False)
                        with VerticalScroll(id="context"):
                            yield Body(text.Links(text.linkify(said)), markup=False)
                # Jev's review, always open beside the text.
                with VerticalScroll(id="jev-side"):
                    yield Static(self._review_text(None), id="review")
                    yield Static(("grades when you pause typing, or " if self.cfg.review_auto else "")
                                 + pretty(self.keys["review"]), id="review-status")
            yield Static(self._hint(), id="compose-hint", classes="note")

    def _hint(self, status: str = "") -> str:
        k = {n: pretty(v) for n, v in self.keys.items()}
        return (f"{k['send']} send  ·  Esc minimize  ·  {k['discard']} discard  ·  {k['sender']} from"
                f"  ·  {k['review']} Jev  ·  Tab next field"
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
        self._outside_timer = self.set_interval(self.OUTSIDE_EVERY, self._outside)

    def on_screen_resume(self):
        if self._closing:
            self.dismiss("gone")

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
    def _changed(self, event):
        if not self._ready and event is not None:
            return
        if self._local_timer:
            self._local_timer.stop()
        self._local_timer = self.set_timer(self.LOCAL_DELAY, self._save_local)
        if isinstance(event, TextArea.Changed):
            self.query_one("#review").set_class(event.text_area.text != self._reviewed, "stale")
            if self.cfg.review_auto:
                if self._review_timer:
                    self._review_timer.stop()
                self._review_timer = self.set_timer(self.REVIEW_DELAY, lambda: self.action_review(quiet=True))

    def _save(self) -> bool:
        """Store what's on screen as the next revision, merging in whatever was saved outside
        since. False when the draft is gone (sent or discarded outside) and this screen closed."""
        for _ in range(5):
            self._collect()
            d = self.draft
            if drafts.fields(d) == self._base:
                return True            # nothing new: never rewrite the saved version
            try:
                drafts.save(d)
            except drafts.Changed as e:
                self._merge(e.draft)
                continue
            except drafts.Gone:
                if not self._gone():
                    return False
                continue
            self._base = drafts.fields(d)
            return True
        return True

    def _outside(self):
        """Show a change the `mail` command made to this draft, or close it once sent / discarded."""
        d = self.draft
        disk = drafts.load(d.id)
        if disk is None:
            if d.rev and self._gone():
                self._changed(None)
        elif disk.rev != d.rev:
            self._merge(disk)
        elif disk.gmail_msgid != d.gmail_msgid:
            d.gmail_msgid, d.synced = disk.gmail_msgid, disk.synced

    def _merge(self, disk: drafts.Draft):
        """Take a newer saved version: each field I haven't touched since gets its text; a field
        both of us changed keeps mine (and the next save stores it on top of theirs)."""
        self._collect()
        self._merged = True
        d, base, theirs = self.draft, self._base, drafts.fields(disk)
        mine, clash = drafts.fields(d), []
        self._ready = False            # the widget updates below are not my typing
        for k in drafts.FIELDS:
            if mine[k] == base[k] and theirs[k] != mine[k]:
                self._show_field(k, theirs[k])
            elif mine[k] != base[k] and theirs[k] != base[k] and theirs[k] != mine[k]:
                clash.append(k)
        self.call_after_refresh(lambda: setattr(self, "_ready", True))
        for k in ("quote", "headers", "attachments", "message_id", "gmail_msgid", "synced", "rev", "edited"):
            setattr(d, k, getattr(disk, k))
        self._collect()
        self._base = theirs
        if clash:
            self.notify("The draft was also changed outside the client; your text was kept in "
                        + ", ".join(clash), severity="warning", timeout=10)
            self._changed(None)        # store mine on top of theirs
        else:
            self.notify("The draft was changed outside the client", timeout=3)
            if drafts.fields(d) != theirs:
                self._changed(None)

    def _show_field(self, k: str, value: str):
        if k == "body":
            area = self.query_one("#body", TextArea)
            row, col = area.cursor_location
            area.load_text(value)
            lines = value.split("\n")
            row = min(row, len(lines) - 1)
            area.move_cursor((row, min(col, len(lines[row]))))
        elif k == "ident":
            sel = self.query_one("#from", Select)
            if value in self._idents:
                sel.value = value
        else:
            self.query_one(f"#{k}", Input).value = value

    def _gone(self) -> bool:
        """Sent or discarded outside. Keep what I wrote since as a new draft, else close.
        True when it was kept."""
        self._collect()
        self._lost = True
        d = self.draft
        if drafts.fields(d) == self._base:
            self._stop_timers()
            if self._outside_timer:
                self._outside_timer.stop()
            self.notify("This draft was sent or discarded outside the client", severity="warning", timeout=6)
            if self is self.app.screen:
                self.dismiss("gone")
            else:
                self._closing = True
            return False
        d.id, d.rev, d.synced, d.gmail_msgid = uuid.uuid4().hex, 0, 0, 0
        d.message_id = make_msgid(domain=d.ident.split("@")[-1])
        d.attachments = [a for a in d.attachments if Path(a["path"]).exists()]
        self._base = dict.fromkeys(drafts.FIELDS)
        self.notify("This draft was sent or discarded outside the client; "
                    "what you wrote since stays here as a new draft", severity="warning", timeout=10)
        return True

    def _save_local(self):
        self._collect()
        if self.draft.untouched:       # e.g. a reply's quote rendering: nothing typed yet
            return
        before = self.draft.rev
        if not self._save() or self.draft.rev == before:
            return
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

    # -- Jev review

    def action_review(self, quiet=False):
        body = self.query_one("#body", TextArea).text
        if len(body.strip()) < self.REVIEW_MIN or body == self._reviewed:
            if not quiet:
                self.notify("Write a bit more first" if len(body.strip()) < self.REVIEW_MIN
                            else "Jev already graded this text", timeout=2)
            return
        self.query_one("#review-status", Static).update(Text("reading…", style="dim"))
        self._review(body, self.query_one("#subject", Input).value,
                     quote_context(self.draft.quote)[1] if self.draft.reply else "")

    @work(thread=True, exclusive=True, group="review")
    def _review(self, body: str, subject: str, original: str):
        try:
            grades = jev.review(self.cfg, body, subject, original)
        except jev.JevError as e:
            grades = str(e)
        if not get_current_worker().is_cancelled:     # a newer review took over
            self.app.call_from_thread(self._show_review, body, grades)

    def _criteria(self) -> list[str]:
        return [c.name for c in self.cfg.review or jev.CRITERIA if self.draft.reply or not c.reply_only]

    def _review_text(self, review: "jev.Review | None") -> Text:
        """The sidebar: a bar per criterion (empty before the first review), then what's unanswered."""
        out = Text()
        out.append("Jev\n\n", style="bold")
        names = self._criteria()
        width = max(map(len, names + ["answered"])) + 2
        if self.draft.reply:
            out.append("answered".ljust(width))
            if review and review.asked:
                ok = review.asked - len(review.missed)
                out.append(f"{ok}/{review.asked}", style="green" if not review.missed else "red")
            else:
                out.append("–" if review else "·", style="bright_black")
            out.append("\n")
        for name in names:
            out.append(name.ljust(width))
            g = review.grades.get(name) if review else None
            filled = round(g.score * 5) if g else 0
            colour = "green" if g and g.score >= 0.7 else "yellow" if g and g.score >= 0.4 else "red"
            out.append("━" * filled, style=colour)
            out.append("━" * (5 - filled) + "\n", style="bright_black")
        if review and review.missed:
            out.append("\nNot answered\n", style="bold red")
            for sent in review.missed:
                out.append("✗ ", style="red")
                out.append(sent + "\n")
        return out

    def _show_review(self, body: str, review):
        if not self.is_attached:
            return
        status = self.query_one("#review-status", Static)
        if isinstance(review, str):
            status.update(Text(review, style="red"))
            return
        self._reviewed = body
        status.update("")
        w = self.query_one("#review", Static)
        w.update(self._review_text(review))
        w.set_class(self.query_one("#body", TextArea).text != body, "stale")

    def _stop_timers(self):
        for t in (self._local_timer, self._gmail_timer, self._review_timer):
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
        before = self.draft.rev
        if not self._save():
            return
        if self.draft.rev != before or self.draft.synced != self.draft.rev:
            self._save_gmail()
        self.dismiss("minimized")

    def action_discard(self):
        def yes(ok):
            if not ok or not self.is_attached:
                return
            self._stop_timers()
            d = self.draft
            drafts.forget(d)
            self.store.submit("drafts", lambda: drafts.purge(self.store.drafts_mbox), latest=f"draft:{d.id}")
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
        self._merged = self._lost = False
        self._outside()                # what the `mail` command did a moment ago counts too
        if self._lost or not self._save():     # kept until the send succeeds
            return
        if self._lost:
            return
        if self._merged:
            self.notify("The draft was just changed outside the client: check it, then send again",
                        severity="warning", timeout=8)
            return
        self._stop_timers()
        d.edited = True
        app, cfg, store = self.app, self.cfg, self.store
        ident = cfg.identity_for(d.ident)
        app.notify(f"Sending from {ident.email}…", timeout=2)

        def send():
            smtp.send(cfg, ident, drafts.build(cfg, d))
            drafts.forget(d)
            drafts.purge(store.drafts_mbox)
            return ident.email

        def sent(address):
            app.notify(f"Sent from {address}", timeout=3)
            app.main.paint_drafts()
            app.main.after_send(d)

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


class Body(Static):
    """A message body in the reader or the message a reply quotes: its text can be selected
    with the mouse (Ctrl+C copies) and Ctrl+click opens a link."""

    def get_selection(self, selection):
        if not isinstance(self.content, text.Links):
            return super().get_selection(selection)
        return selection.extract("\n".join(self.content.lines)), "\n"

    def selection_updated(self, selection):
        if not isinstance(self.content, text.Links):
            return super().selection_updated(selection)
        self.content.selection = selection
        self.update(self.content, layout=False)   # drop Textual's cached render

    def on_click(self, event: events.Click):
        href = event.style.link if event.ctrl else None
        if href:
            event.stop()
            self.app.main.open_href(href)


class Attachments(OptionList):
    """A message's attachments: ↑ ↓ past the first / last one carry on through the conversation."""

    def action_cursor_up(self):
        if not self.highlighted:
            self.screen.action_step(-1)
        else:
            super().action_cursor_up()

    def action_cursor_down(self):
        if self.highlighted is None or self.highlighted >= self.option_count - 1:
            self.screen.action_step(1)
        else:
            super().action_cursor_down()


class Reader(VerticalScroll):
    """Keys move instantly: Textual's scroll animation looks jerky in a terminal."""

    BINDINGS = [Binding("up", "screen.step(-1)", show=False), Binding("down", "screen.step(1)", show=False),
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
        self.views: list[Folder] = []     # every folder, for Ctrl+P
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
        self._last_sync = time.time()     # when a sync of the view was last asked for
        self._push_timer = None
        self._pushed: set[str] = set()    # push kinds waiting for the debounce
        self._cats_mtime = None
        self._stamp = cache.changes_since(0)[0]   # how far the cache's change notes are read

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
            (k["palette"], "palette", "Go to", True),
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
        self.set_interval(10, self.poll)
        self.set_interval(1.5, self._watch_stamp)
        self.store.watch(self.on_push)
        self.apply_layout()
        self.paint_drafts()
        self.store.submit("drafts", lambda: drafts.tidy(self.store.drafts_mbox))

    # -- helpers

    def fail(self, prefix=""):
        def report(e):
            self.notify(f"{prefix}{e}", severity="error", timeout=8)
        return report

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
        hint.update(f"Esc back   ↑↓ scroll   Enter open   Tab links   {k['reply']} reply   {k['forward']} forward   "
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
            folders = self.store.sync.folders(refresh=True)
            counts = self.store.sync.unseen_all()
            self.store.save_folders(folders, counts)
            return folders, counts
        self.store.submit("sync", fetch, self._show_folders, self.fail("Cannot reach Gmail: "))

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
            tabs.append(self.view)       # Sent, Bin, … opened via Ctrl+P
        return tabs

    def _paint_tabs(self):
        tabs = self._tab_list()
        cur = next((i for i, t in enumerate(tabs) if self.view and t.raw == self.view.raw), 0)
        items = [(t.name, self._count(t)) for t in tabs]
        if self.search_query:
            items.append((f"🔍 {self.search_query}", 0))
            cur = len(items) - 1
        self.query_one(TabBar).set_tabs(items, cur, f"←→ tabs  {pretty(self.keys['palette'])} go to")

    def _paint_counts(self):
        self._paint_tabs()

    def refresh_counts(self):
        def fetch():
            counts = self.store.sync.unseen_all()
            folders = self.store.sync.folders()
            self.store.save_folders(folders, counts)
            return counts

        def done(counts):
            self.counts = counts
            self._paint_counts()
        self.store.submit("sync", fetch, done, latest="counts")

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

    def action_palette(self):
        """Go to a folder or a conversation of this list, search all mail, or (after ">") run a command."""
        folders = []
        for f in self.views:
            n = self._count(f)
            folders.append({"text": f.name, "detail": str(n) if n else "", "kind": "folder", "id": f.raw,
                            "style": "bold" if n else "", "boost": 3})
        convs = [{"text": c.subject or "(no subject)", "detail": f"{c.sender}  ·  {short_date(c.date)}",
                  "kind": "conv", "id": c.msgid, "style": "" if c.seen else "bold"} for c in self.msgs]
        cmds = [{"text": name, "detail": help_, "kind": "cmd", "id": fn, "prefix": Text("> ", style="dim")}
                for name, fn, help_ in self.app.commands()]

        def source(q):
            if q.startswith(">"):
                return rank(cmds, q[1:])
            if not q.strip():
                return folders + convs
            return rank(folders + convs, q) + [{"text": f"Search all mail for “{q.strip()}”", "kind": "search",
                                                 "id": q.strip(), "prefix": Text("⌕ ", style="dim")}]

        def chosen(it):
            if not it:
                return
            if it["kind"] == "folder":
                f = next((f for f in self.views if f.raw == it["id"]), None)
                if f:
                    self.open_view(f)
                    self.focus_pane("list")
            elif it["kind"] == "conv":
                i = next((i for i, c in enumerate(self.msgs) if c.msgid == it["id"]), None)
                if i is not None:
                    self.query_one(Messages).move_cursor(row=i)
                    self.open_message(self.msgs[i])
            elif it["kind"] == "search":
                self.search(it["id"])
            else:
                self.call_later(it["id"])
        self.app.push_screen(Palette("Go to a folder or conversation  ·  “>” for commands", source,
                                     "Folder, subject or sender… (> for commands)"), chosen)

    # -- message list

    def open_view(self, view: Folder, keep_cursor=False):
        self.view, self.search_query = view, None
        self.selected.clear()
        self._paint_tabs()
        cached = self.store.cached_list(view.raw)
        if cached is not None:
            cached = self._overlay(cached, 0, view.raw)     # local changes Gmail may not have yet
        self.show_messages(cached or [], keep_cursor and cached is not None)
        self.load_messages(keep_cursor=True)

    def load_messages(self, keep_cursor=False, force=False):
        """Sync the current view from Gmail in the background and repaint if it changed.
        Unless forced, one STATUS round trip settles that nothing changed."""
        view, query = self.view, self.search_query
        if view is None and not query:
            return
        self._last_sync = time.time()

        def fetch():
            started = time.time()
            msgs = self.store.sync.search(query, limit=300) if query else \
                self.store.messages(view.raw, limit=300, force=force)
            return started, msgs

        def done(res):
            started, msgs = res
            if msgs is None:
                return                  # unchanged in Gmail
            msgs = self._overlay(msgs, started, f"?{query}" if query else view.raw)
            if not query:
                self.store.save_list(view.raw, msgs)
            if (view, query) != (self.view, self.search_query):
                return  # the user moved on meanwhile
            if self._same(msgs):
                return
            old = {m.msgid for m in self.raw}
            self.show_messages(msgs, keep_cursor)
            if query and not msgs:
                self.notify("Nothing found")
            self._got_new([m for m in msgs if m.msgid not in old] if old else [])
        self.store.submit("sync", fetch, done, self._sync_failed, latest="view")
        self.schedule_prefetch(delay=0.1)

    def _sync_failed(self, e):
        """A background sync that fails (offline) says so once, not on every retry."""
        if time.time() - getattr(self, "_failed_at", 0) > 300:
            self.notify(f"{e}", severity="error", timeout=8)
        self._failed_at = time.time()

    def _got_new(self, new: list[Msg]):
        """Messages a sync just brought: their bodies and conversations download now, wherever the
        cursor is, and a conversation open in the reader gets its new replies."""
        if not new:
            return
        thrids = list(dict.fromkeys(m.thrid for m in new if m.thrid))

        def job():
            self.store.prefetch(new)
            self.store.prefetch_threads(thrids, listed=new)
        self.store.submit("bg", job, latest="prefetch-new")
        c = self.current
        if c and c.thrid and any(m.thrid == c.thrid for m in new) \
                and {m.msgid for m in new if m.thrid == c.thrid} - {m.msgid for m in self.thread_msgs}:
            self._refresh_thread()

    def _refresh_thread(self):
        """Fetch the open conversation again and merge it into the reader in place (new messages
        come in folded, nothing moves or takes the focus)."""
        c = self.current
        if not c or not c.thrid:
            return

        def fetch():
            # the sync lane: a new reply must not wait behind a 40-body prefetch on `bg`
            full = self.store.sync.thread([c.thrid])
            if full:
                self.store.save_thread(c.thrid, full)
                self.store.prefetch(full, mbox=self.store.sync)
            return full

        def done(full):
            if self.current is c and full:
                self.update_reader(full, follow=False)
        self.store.submit("sync", fetch, done, latest="reader")

    def _hidden(self, p: dict, raw: str | None) -> bool:
        """A pending removal hides the message only where it was removed from (an archived one
        still shows in All Mail, a moved one in its new category); a deleted one everywhere but Bin."""
        where = p.get("hide")
        if where == "*":
            return not any(f.raw == raw and f.role == "trash" for f in self.views)
        return bool(where) and where == raw

    def _overlay(self, msgs: list[Msg], started: float, raw: str | None) -> list[Msg]:
        """Re-apply local changes that this sync result (of folder `raw`, "?query" for a search) may predate."""
        out = []
        for m in msgs:
            p = self.pending.get(m.msgid)
            if p and p["done"] is not None and started and p["done"] + PENDING_GRACE < started:
                del self.pending[m.msgid]   # Gmail had it well before this sync began
                p = None
            if p and self._hidden(p, raw):
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
            self.pending[i] = {"hide": None, "seen": None, "flag": None, "done": None, **change}
        return ids

    def _persist(self):
        """Save the list as shown, so a tab switch or a restart paints it the same way."""
        if self.view and not self.search_query:
            self.store.save_list(self.view.raw, self.raw)

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
                self.store.prefetch_threads(thrids, listed=window)
            self.store.submit("bg", job, latest="prefetch")

    # -- reader: the conversation, oldest first, every message collapsed to one line

    def open_message(self, c: Conv):
        if self.view and self.view.role == "drafts" and not self.search_query:
            self.continue_gmail_draft(c.msgs[-1])
            return
        self._opening = c.msgid
        self._follow_latest = True     # until the user moves, the newest message stays open
        full = self.store.cached_thread(c.thrid) or (list(c.msgs) if not c.thrid else None)
        if full and c.thrid:
            # A cached draft is likely sent or discarded by now; the fetch below brings back live ones.
            full = [m for m in full if "\\Draft" not in m.labels] or None
        if full and c.thrid:
            # The list may know a reply the cached conversation doesn't yet: show it in the first paint.
            have = {m.msgid for m in full}
            in_all = not (self.view and not self.search_query and self.view.role in ("trash", "spam"))
            extra = [m for m in c.msgs if in_all and m.msgid not in have and "\\Draft" not in m.labels]
            full = sorted(full + extra,
                          key=lambda m: (m.date.timestamp() if m.date else 0, m.uid))
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

        reading = {m.msgid for m in c.unread}    # marked read below; the saved copy must agree

        def fetch():
            full = self.mbox.thread([c.thrid]) or list(c.msgs)
            for m in full:
                if m.msgid in reading:
                    m.flags.add("\\Seen")
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
            self._persist()
            views = list(self.views)

            def mark():
                self.mbox.set_seen(unread, True)
                cache.reflect({"kind": "seen", "msgids": ids, "on": True}, folders=views)
            self.fg(mark, lambda _: (self._settle(ids), self.refresh_counts()))

    def continue_gmail_draft(self, m: Msg):
        local = next((d for d in drafts.load_all()
                      if d.gmail_msgid == m.msgid or (m.message_id and d.message_id == m.message_id)), None)
        # X-GM-MSGIDs only grow: a lower one is an older copy, not an edit made on the phone.
        if local and (m.msgid <= local.gmail_msgid or local.synced != local.rev):
            self.open_draft(local)     # the same version, or mine has changes Gmail hasn't got yet
            return
        if local:                      # changed on the phone since: continue with that version
            def adopt(parsed):
                self.open_draft(drafts.adopt(self.cfg, local, m, parsed))
            raw = self.store.cached_body(m)
            if raw is not None:
                adopt(parse(raw))
            else:
                self.fg(lambda: self.mbox.fetch(m), adopt)
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

    def update_reader(self, full: list[Msg], follow: bool | None = None):
        """Merge the full thread (sent replies, freshly downloaded bodies) into what is on screen.
        follow=False (a push while reading): new messages come in folded and nothing moves."""
        box = self.query_one("#thread", Vertical)
        for m in full:
            if m.msgid not in self.bodies:
                raw = self.store.cached_body(m)
                if raw is not None:
                    self.bodies[m.msgid] = parse(raw)
        shown = {w.msg.msgid: w for w in self._message_widgets()}
        keep = {m.msgid for m in full}
        refocus = False
        for msgid, w in list(shown.items()):
            if msgid not in keep:                           # gone from Gmail (a sent or discarded draft)
                refocus |= w.has_focus_within
                w.remove()
                del shown[msgid]
        old_newest = self.thread_msgs[-1].msgid if self.thread_msgs else None
        new_newest = full[-1].msgid if full else None
        follow = follow is not False and getattr(self, "_follow_latest", False) and new_newest != old_newest
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
        if refocus and not follow and new_newest:
            self.call_after_refresh(self._focus_message, new_newest)
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
            kids.append(Attachments(*[Option(f"📎 {text.attachment_name(p)}  "
                                            f"{len(p.get_payload(decode=True) or b'') // 1024} kB", id=str(i))
                                     for i, p in enumerate(parts)], classes="attachments"))
        if is_md and main:
            body = text.Links(Markdown(main))
        else:
            body = text.Links(text.linkify(text.reflow(main))) if main else "(empty)"
        kids.append(Body(body, markup=False, classes="body"))
        if quoted:
            kids.append(Collapsible(Body(text.Links(text.linkify(quoted)), markup=False, classes="history"),
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

    STEP = 2   # lines per ↑ ↓ inside a long message

    def _span(self, w) -> tuple[int, int]:
        """Where `w` lies on the reader's whole page, scrolled out parts included: (top, bottom)."""
        reader = self.query_one(Reader)
        y, node = w.virtual_region.y, w.parent
        while node is not None and node is not reader:
            y += node.virtual_region.y + node.styles.gutter.top   # children sit inside its padding
            node = node.parent
        return y, y + w.virtual_region.height

    def action_step(self, step: int):
        """↑ ↓ in the reader: scroll through the open message; at its edge go to the previous / next one."""
        reader = self.query_one(Reader)
        self._follow_latest = False
        w = next((w for w in self._message_widgets() if w.has_focus_within), None)
        if w is not None and not w.collapsed:
            top, bottom = self._span(w)
            view_top = round(reader.scroll_y)
            view_bottom = view_top + reader.scrollable_content_region.height
            if step > 0 and bottom > view_bottom and reader.scroll_y < reader.max_scroll_y:
                reader.scroll_relative(y=min(self.STEP, bottom - view_bottom), animate=False)
                return
            if step < 0 and top < view_top and reader.scroll_y > 0:
                reader.scroll_relative(y=-min(self.STEP, view_top - top), animate=False)
                return
        ws = self._message_widgets()
        if w is not None and w is (ws[0] if step < 0 else ws[-1]):
            reader.scroll_relative(y=step * self.STEP, animate=False)   # the headers above, the margin below
            return
        self.action_message(step)

    def action_message(self, step: int):
        """Previous / next message of the conversation."""
        ws = self._message_widgets()
        if not ws:
            return
        self._follow_latest = False
        cur = next((i for i, w in enumerate(ws) if w.has_focus_within), len(ws) - 1)
        to = ws[max(0, min(len(ws) - 1, cur + step))]
        self._focus_message(to.msg.msgid)
        reader = self.query_one(Reader)
        height = reader.scrollable_content_region.height
        if step < 0 and to is not ws[cur] and not to.collapsed and to.virtual_region.height > height:
            # Reading upwards into a long message: carry on from its end, not its top.
            reader.scroll_to(y=self._span(to)[1] - height, animate=False)

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
        self.open_href(s.content.links[i][0])

    def open_href(self, href: str):
        """Open a link from a message: mailto: in a new message, a Trello card or board in the Trello
        client when it's installed, anything else in the browser."""
        if href.startswith("mailto:"):
            u = urlparse(href)
            q = parse_qs(u.query)
            ident = self.cfg.identities[0].email if self.cfg.identities else self.cfg.email
            self.open_compose(ident=ident, to=unquote(u.path), cc=",".join(q.get("cc", [])),
                              subject=q.get("subject", [""])[0], body=q.get("body", [""])[0])
            return
        cmd = ["xdg-open", href]
        if TRELLO_LINK.match(href) and shutil.which("trello"):
            cmd = ["sh", "-c", 'trello open "$1" || xdg-open "$1"', "sh", href]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.notify(f"Opened {href[:60]}", timeout=2)

    @on(OptionList.OptionSelected, ".attachments")
    def _open_attachment(self, event: OptionList.OptionSelected):
        col = next(a for a in event.option_list.ancestors if isinstance(a, Collapsible))
        part = text.attachments(self.bodies[col.msg.msgid])[int(event.option.id)]
        folder = CACHE_DIR / "attachments"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / text.attachment_name(part)
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
        self.load_messages(keep_cursor=True, force=True)

    def poll(self):
        """A safety net under push: rare while the INBOX watcher is connected, frequent when not."""
        every = POLL_PUSHED if self.store.watching() else POLL_ALONE
        if time.time() - self._last_sync >= every:
            self.kick()

    def kick(self, force=False):
        """Sync now: counts, and the view unless rows are selected (that sync waits for the next kick)."""
        self._last_sync = time.time()
        self.refresh_counts()
        if not self.selected:
            self.load_messages(keep_cursor=True, force=force)

    def kick_if_stale(self, age: float = 5):
        """Coming back to the window: catch up if the last sync is older than `age` seconds."""
        if time.time() - self._last_sync > age:
            self.kick()

    def on_screen_resume(self):
        self.kick_if_stale()

    def on_push(self, kind: str):
        """The INBOX watcher saw something: sync a moment later (changes come in bursts)."""
        self._pushed.add(kind)
        if self._push_timer:
            self._push_timer.stop()
        self._push_timer = self.set_timer(0.15, self._pushed_sync)

    def _pushed_sync(self):
        kinds, self._pushed = self._pushed, set()
        if "new" in kinds:
            cache.start_sort()        # Jev files it within seconds, not at the next timer tick
        inbox = next((v for v in self.views if v.role == "inbox"), None)
        here = bool(inbox and self.view and not self.search_query and self.view.raw == inbox.raw)
        # New mail (or a gap while disconnected) forces a full read; a flag or expunge (often the
        # echo of our own action) is settled by one STATUS round trip.
        self.kick(force=here and bool(kinds & {"new", "reconnected"}))
        if inbox and not here:
            self._sync_cached(inbox)

    def _sync_cached(self, folder: Folder):
        """Bring a folder that isn't on screen up to date on disk, with the new mail's bodies,
        so switching to it shows the fresh list and opens it at once."""
        def job():
            before = {m.msgid for m in self.store.cached_list(folder.raw) or []}
            msgs = self.store.messages(folder.raw, limit=300)
            if msgs is None:
                return
            self.store.save_list(folder.raw, msgs)
            new = [m for m in msgs if m.msgid not in before]
            if new and before:
                self.store.submit("bg", lambda: (self.store.prefetch(new),
                                                 self.store.prefetch_threads([m.thrid for m in new], listed=new)),
                                  latest="prefetch-new")
        self.store.submit("sync", job, latest=f"cached:{folder.raw}")

    def _categories_changed(self):
        """categories.toml edited outside (`mail cat`, by hand): show its tabs."""
        try:
            mtime = (config.CONFIG_DIR / "categories.toml").stat().st_mtime
        except OSError:
            return
        if self._cats_mtime is None:
            self._cats_mtime = mtime
        elif mtime != self._cats_mtime:
            self._cats_mtime = mtime
            try:
                self.cfg.categories = config.load_categories()
            except (ValueError, TypeError) as e:   # a typo while editing it by hand
                self.notify(f"categories.toml: {e}", severity="error", timeout=8)
                return
            self.load_folders()

    def _watch_stamp(self):
        """The `mail` command (or Jev's sort) changed the cache: show it, then check with Gmail."""
        self._categories_changed()
        pos, others = cache.changes_since(self._stamp)
        if pos == self._stamp:
            return
        self._stamp = pos
        if not others:
            return
        if self.view and not self.search_query and not self.selected:
            cached = self.store.cached_list(self.view.raw)
            if cached is not None:
                cached = self._overlay(cached, 0, self.view.raw)
                if not self._same(cached):
                    self.show_messages(cached, keep_cursor=True)
        self.paint_drafts()
        self.kick(force=True)

    def action_help(self):
        self.app.push_screen(Help(self.keys))

    # -- actions: mail (applied on screen at once, sent to Gmail in order behind it)

    def _apply(self, convs: list[Conv], fn, remove_rows=True, everywhere=False):
        """Act on whole conversations: shown at once, then every message of each thread
        (incl. sent replies, looked up in All Mail) is changed in Gmail behind it, and in
        every cached list. everywhere=True (Bin): gone from every view, not just this one."""
        if not convs:
            return
        ids = []
        view_raw = self.view.raw if self.view and not self.search_query else None
        if remove_rows:
            members = self.members(convs)
            unread = sum(1 for m in members if not m.seen)
            here = view_raw or (f"?{self.search_query}" if self.search_query else None)
            ids = self._mark_pending(members, hide="*" if everywhere else here)
            self.drop_rows(convs)
            if self.view and unread:
                self.bump_count(self.view.raw, -unread)
        views = list(self.views)

        def job():
            full = self.mbox.thread([c.thrid for c in convs])
            known = {m.thrid for m in full}
            full += [m for c in convs if c.thrid not in known for m in c.msgs]
            rec = fn(full)
            cache.reflect(rec, full, folders=views)
            rec["_msgs"] = full          # in memory only: what an undo puts back on screen
            return rec

        def done(rec):
            self._settle(ids)
            if remove_rows and view_raw:
                rec["_rows"], rec["_view"] = convs, view_raw
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
        self._apply(convs, lambda full: ops.trash(self.mbox, full, view), everywhere=True)

    def action_undo(self):
        if not self.undo_stack:
            self.notify("Nothing to undo")
            return
        rec = self.undo_stack.pop()
        for i in rec.get("msgids") or [it["msgid"] for it in rec.get("items", [])]:
            self.pending.pop(i, None)
        rows = rec.get("_rows")
        if rows and self.view and not self.search_query and rec.get("_view") == self.view.raw:
            # Put the rows back at once; Gmail follows behind.
            have = {m.msgid for m in self.raw}
            back = [m for c in rows for m in c.msgs if m.msgid not in have]
            self.show_messages(sorted(self.raw + back, key=lambda m: (m.date.timestamp() if m.date else 0, m.uid),
                                      reverse=True), keep_cursor=True)
            self._persist()
            self.bump_count(self.view.raw, sum(1 for m in back if not m.seen))
        views = list(self.views)

        def job():
            msg = ops.undo(self.mbox, rec)
            cache.reflect(cache.inverse(rec), rec.get("_msgs", ()), folders=views)
            return msg

        def done(msg):
            self.notify(msg, timeout=3)
            self.load_messages(keep_cursor=True, force=True)
            self.refresh_counts()
        self.fg(job, done)

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
        self._persist()
        views = list(self.views)

        def job():
            if on:
                # the message just marked on screen, so a later sync agrees with it
                rec = ops.star(self.mbox, [m for m in changed], True)
            else:
                full = self.mbox.thread([c.thrid for c in convs])
                known = {m.thrid for m in full}
                full += [m for c in convs if c.thrid not in known for m in c.msgs]
                rec = ops.star(self.mbox, full, False)
            cache.reflect(rec, folders=views)
            return rec

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
        self._persist()
        views = list(self.views)

        def job():
            ops.set_read(self.mbox, changed, seen)
            cache.reflect({"kind": "seen", "msgids": ids, "on": seen}, folders=views)
        self.fg(job, lambda _: (self._settle(ids), self.refresh_counts()))

    def search(self, q: str):
        self.search_query = q
        self.selected.clear()
        self._paint_tabs()
        self.show_messages([])
        self.load_messages()
        self.query_one(Messages).focus()

    def action_search(self):
        def got(q):
            if q:
                self.search(q)
        self.app.push_screen(Ask("Search all mail", self.search_query or "",
                                 "from:jan has:attachment newer_than:30d to:me@example.com"), got)

    def jev_history(self):
        entries = ops.sort_history(40)
        if not entries:
            self.notify("Jev hasn't moved anything yet")
            return
        opts = [(str(i), Text.assemble((datetime.fromtimestamp(e["ts"]).strftime("%-d %b %H:%M  "), "dim"),
                                       (f"{e['choice']:<14}", "bold"), f"{e['subject'][:60]}"))
                for i, e in enumerate(entries)]

        views = list(self.views)

        def picked(i):
            if i is None:
                return
            e = entries[int(i)]

            def job():
                msg = ops.undo(self.mbox, e["undo"])
                cache.reflect(cache.inverse(e["undo"]), folders=views)
                return msg

            def done(msg):
                ops.mark_undone(e["msgid"])
                self.notify(f"Back in Inbox: {e['subject'][:50]}")
                self.load_messages(keep_cursor=True, force=True)
                self.refresh_counts()
            self.fg(job, done)
        self.app.push_screen(Picker("Jev moved these — Enter puts one back in Inbox", opts), picked)

    # -- compose

    def open_compose(self, ident: str, attach=None, **kw):
        self.open_draft(drafts.new(ident, attach_parts=attach, **kw))

    def open_draft(self, d: drafts.Draft):
        def closed(result):
            self.paint_drafts()
            if result == "minimized":
                self.notify(f"Draft saved · {pretty(self.keys['drafts'])} to continue", timeout=3)
        self.app.push_screen(Compose(self.cfg, self.keys, d, self.store), closed)

    def after_send(self, d: drafts.Draft):
        """Gmail files a sent message into Sent and its conversation a moment after SMTP is
        done: look again shortly (and once more), so the row shows "me" and the reader the reply."""
        refs = set(d.headers.get("In-Reply-To", "").split() + d.headers.get("References", "").split())
        thrid = next((m.thrid for m in self.thread_msgs + self.raw if m.message_id and m.message_id in refs), 0)
        cache.drop_thread(thrid)

        def again():
            self.kick(force=True)
            if thrid and self.current and self.current.thrid == thrid:
                self._refresh_thread()
        self.set_timer(1.5, again)
        self.set_timer(6, again)

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
                fwd = (f"{text.FORWARD_HEAD}\nFrom: {m.sender} <{m.sender_addr}>\nDate: {when}\n"
                       f"Subject: {m.subject}\nTo: {m.to}\n" + (f"Cc: {m.cc}\n" if m.cc else "") + f"\n{body}")
                self.open_compose(ident=ident.email, subject=subj, quote=fwd, attach=text.attachments(parsed))
                return
            if not subj.lower().startswith("re:"):
                subj = "Re: " + subj
            reply_to = text.header_addr(parsed, "Reply-To") or formataddr((m.sender, m.sender_addr))
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
                    self.cfg.categories = config.edit_categories(
                        lambda cats: [c for c in cats if c.name != name] + [Category(name, desc or "")])

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

                    def rename(cats):
                        for c in cats:
                            if c.name == cat.name:
                                c.name, c.description = new, desc if desc is not None else c.description
                        return cats
                    self.cfg.categories = config.edit_categories(rename)

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
                self.cfg.categories = config.edit_categories(lambda cats: [c for c in cats if c.name != cat.name])
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
    ENABLE_COMMAND_PALETTE = False     # Ctrl+P is the client's own go-to (MainScreen.action_palette)

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
    .compose { width: 100%; height: 100%; border: round $accent; background: $surface;
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
    .compose #compose-main { height: 1fr; }
    .compose #compose-left { width: 1fr; }
    .compose #jev-side { width: 34; margin-left: 2; padding: 0 0 0 2; border-left: vkey ansi_bright_black;
                         scrollbar-size-vertical: 1; }
    .compose #review-status { color: $text-muted; padding-top: 1; }
    .compose #review.stale { text-opacity: 50%; }
    .compose .context-head { color: $text-muted; margin-top: 1; text-wrap: nowrap; text-overflow: ellipsis; }
    .compose #context { height: auto; max-height: 35%; color: $text-muted; padding: 0 0 0 1;
                        border-left: outer ansi_bright_black; scrollbar-size-vertical: 1; }
    /* The ANSI theme leaves selections the same colour as the background: make them visible. */
    TextArea > .text-area--selection { background: ansi_blue; color: ansi_black; text-style: none; }
    Input > .input--selection { background: ansi_blue; color: ansi_black; }
    Screen > .screen--selection { background: ansi_blue; color: ansi_black; }
    """

    def __init__(self, cfg: Config, compose: str | None = None):
        super().__init__()
        self.cfg, self.compose_url = cfg, compose
        self.store = Store(cfg, self._call_ui)
        self.mbox = self.store.fg

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

    def commands(self) -> list[tuple[str, object, str]]:
        """What Ctrl+P lists after ">": (name, function, description)."""
        m = self.main
        return [
            ("Jev history", m.jev_history, "Recent automatic moves; put one back"),
            ("Sort inbox with Jev now", self.sort_now, "Run the auto-sort once"),
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
            ("Add category", m.action_add_category, "Creates the Gmail label"),
            ("Remove category", m.remove_category, "Deletes the current tab's Gmail label; mail stays in All Mail"),
            ("Rename category", m.action_rename_category, ""),
            ("Refresh", m.action_refresh, ""),
            ("Keyboard shortcuts", m.action_help, ""),
            ("Quit", self.exit, "Close the mail client"),
        ]

    def sort_now(self):
        m = self.main
        lines = []

        def sort():
            with ops.sort_lock() as mine:
                return ops.sort_inbox(self.cfg, self.store.sort_mbox, report=lines.append) if mine else None

        def done(n):
            if n is None:
                self.notify("Jev is sorting already")
                return
            self.notify(f"Jev moved {n} message(s)" if n else "Jev: nothing to move")
            m.kick(force=True)
        self.store.submit("sort", sort, done, m.fail("Jev: "))

    def on_app_focus(self, event):
        if getattr(self, "main", None):
            self.main.kick_if_stale()

    def _call_ui(self, fn, *args):
        try:
            self.call_from_thread(fn, *args)
        except Exception:  # noqa: BLE001 - app already closing, or a bug in a callback
            if self.is_running:
                config.log_error(f"ui callback {getattr(fn, '__qualname__', fn)}")

    def on_unmount(self):
        self.store.close()
        self.store.prune_bodies()
