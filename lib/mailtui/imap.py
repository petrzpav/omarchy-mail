"""Gmail over IMAP: folders, message lists, bodies and label changes.

Categories are Gmail labels, so "move to a category" means adding its label
and dropping the one of the view it came from (plus \\Inbox). Every call goes
through one connection guarded by a lock, so the TUI's worker threads and the
refresh timer can share it; a dropped connection is reopened once and retried.
"""

import base64
import email
import email.policy
import imaplib
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from .config import Config

HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID DELIVERED-TO X-FORWARDED-TO X-ORIGINAL-TO"
SPECIAL = {"\\All": "all", "\\Trash": "trash", "\\Sent": "sent", "\\Junk": "spam",
           "\\Drafts": "drafts", "\\Flagged": "starred", "\\Important": "important"}
# Gmail system labels as X-GM-LABELS spells them, keyed by the special-use role.
SYSTEM_LABEL = {"inbox": "\\Inbox", "sent": "\\Sent", "starred": "\\Starred",
                "important": "\\Important", "drafts": "\\Draft"}


# ------------------------------------------------------------ encoding helpers

def utf7_encode(s: str) -> str:
    """IMAP modified UTF-7 (RFC 3501 5.1.3) for mailbox names."""
    out, buf = [], []

    def flush():
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode()
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def utf7_decode(s: str) -> str:
    def repl(m):
        if not m.group(1):
            return "&"
        b = m.group(1).replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", repl, s)


def quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).replace("\r", "").replace("\n", " ")
    except Exception:
        return str(value)


def _tokens(s: str) -> list[str]:
    out = []
    for q, bare in re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', s):
        out.append(re.sub(r"\\(.)", r"\1", q) if q or not bare else bare)
    return out


# ------------------------------------------------------------ data

@dataclass
class Folder:
    name: str          # display name (decoded)
    raw: str           # IMAP name (modified UTF-7)
    role: str = ""     # inbox/all/trash/sent/spam/drafts/starred/important or "" for labels

    @property
    def label(self) -> str | None:
        """How X-GM-LABELS spells this folder, or None when it isn't a label."""
        if self.role in SYSTEM_LABEL:
            return SYSTEM_LABEL[self.role]
        return None if self.role else self.name


@dataclass
class Msg:
    folder: str        # raw IMAP folder the uid belongs to
    uid: int
    msgid: int         # X-GM-MSGID, stable across folders
    thrid: int = 0     # X-GM-THRID, the Gmail conversation
    flags: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    sender: str = ""
    sender_addr: str = ""
    subject: str = ""
    date: datetime | None = None
    to: str = ""
    cc: str = ""
    message_id: str = ""
    recipients: list[str] = field(default_factory=list)  # every address it was delivered to
    size: int = 0

    @property
    def seen(self) -> bool:
        return "\\Seen" in self.flags


class ImapError(Exception):
    pass


# ------------------------------------------------------------ mailbox

class Mailbox:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.conn: imaplib.IMAP4_SSL | None = None
        self.selected: str | None = None
        self._folders: list[Folder] | None = None

    # -- connection

    def _connect(self):
        if not self.cfg.password:
            raise ImapError("GMAIL_APP_PASSWORD missing in ~/.config/petrzpav-mail/secrets")
        conn = imaplib.IMAP4_SSL(self.cfg.imap_host, timeout=30)
        try:
            conn.login(self.cfg.email, self.cfg.password)
        except imaplib.IMAP4.error as e:
            raise ImapError(f"Gmail login failed: {e}") from e
        self.conn, self.selected = conn, None

    def _run(self, fn):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.conn is None:
                        self._connect()
                    return fn(self.conn)
                except (imaplib.IMAP4.abort, OSError):
                    self.conn, self.selected = None, None
                    if attempt == 2:
                        raise

    def close(self):
        with self.lock:
            if self.conn:
                try:
                    self.conn.logout()
                except Exception:
                    pass
            self.conn = None

    def _select(self, c, raw: str, readonly=False, fresh=False):
        """fresh=True reopens the folder: an already-open session may not yet report
        messages that another connection moved out (Gmail defers those expunges)."""
        if fresh or self.selected != raw:
            typ, data = c.select(quote(raw), readonly=readonly)
            if typ != "OK":
                raise ImapError(f"cannot open {utf7_decode(raw)}: {data}")
            self.selected = raw

    # -- folders

    def folders(self, refresh=False) -> list[Folder]:
        if self._folders is not None and not refresh:
            return self._folders

        def go(c):
            typ, data = c.list()
            out = []
            for line in data:
                m = re.match(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', line)
                if not m:
                    continue
                flags = m.group("flags").decode().split()
                if "\\Noselect" in flags:
                    continue
                raw = m.group("name").decode()
                raw = _tokens(raw)[0] if raw.startswith('"') else raw
                role = "inbox" if raw.upper() == "INBOX" else next(
                    (SPECIAL[f] for f in flags if f in SPECIAL), "")
                name = "Inbox" if role == "inbox" else utf7_decode(raw).removeprefix("[Gmail]/")
                out.append(Folder(name=name, raw=raw, role=role))
            return out

        self._folders = self._run(go)
        return self._folders

    def folder(self, role: str) -> Folder:
        f = next((f for f in self.folders() if f.role == role), None)
        if not f:
            raise ImapError(f"no {role} folder")
        return f

    def label_folder(self, name: str) -> Folder | None:
        return next((f for f in self.folders() if not f.role and f.name == name), None)

    def unseen(self, raw: str) -> int:
        def go(c):
            typ, data = c.status(quote(raw), "(UNSEEN)")
            m = re.search(rb"UNSEEN (\d+)", data[0] or b"")
            return int(m.group(1)) if m else 0
        return self._run(go)

    # -- listing

    def _parse_fetch(self, folder: str, data) -> list[Msg]:
        msgs, cur, meta = [], None, b""

        def finish():
            if cur is None:
                return
            text = meta.decode(errors="replace")
            m = re.search(r"UID (\d+)", text)
            if not m:
                return
            msg = Msg(folder=folder, uid=int(m.group(1)), msgid=0)
            if m := re.search(r"X-GM-MSGID (\d+)", text):
                msg.msgid = int(m.group(1))
            if m := re.search(r"X-GM-THRID (\d+)", text):
                msg.thrid = int(m.group(1))
            if m := re.search(r"FLAGS \(([^)]*)\)", text):
                msg.flags = set(m.group(1).split())
            if m := re.search(r"X-GM-LABELS \((.*?)\)(?: [A-Z]|\)|$)", text):
                msg.labels = set(_tokens(m.group(1)))
            if m := re.search(r"RFC822\.SIZE (\d+)", text):
                msg.size = int(m.group(1))
            h = email.message_from_bytes(cur or b"")
            name, addr = parseaddr(decode(h["From"]))
            msg.sender, msg.sender_addr = name or addr, addr
            msg.subject = decode(h["Subject"]) or "(no subject)"
            msg.to, msg.cc = decode(h["To"]), decode(h["Cc"])
            msg.message_id = (h["Message-ID"] or "").strip()
            try:
                msg.date = parsedate_to_datetime(h["Date"]) if h["Date"] else None
                if msg.date and msg.date.tzinfo is None:
                    msg.date = msg.date.astimezone()
            except Exception:
                msg.date = None
            fields = [decode(h[k]) for k in ("To", "Cc", "Delivered-To", "X-Forwarded-To", "X-Original-To")]
            msg.recipients = [a.lower() for _, a in getaddresses([f for f in fields if f]) if a]
            msgs.append(msg)

        for item in data:
            if isinstance(item, tuple):
                finish()
                meta, cur = item[0], item[1]
            elif isinstance(item, bytes) and cur is not None:
                meta += item
        finish()
        return msgs

    def _fetch_headers(self, c, folder: str, uidset: str) -> list[Msg]:
        typ, data = c.uid("FETCH", uidset,
                          f"(UID FLAGS X-GM-MSGID X-GM-THRID X-GM-LABELS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])")
        if typ != "OK":
            raise ImapError(f"fetch failed: {data}")
        msgs = self._parse_fetch(folder, [d for d in data if d])
        msgs.sort(key=lambda m: (m.date.timestamp() if m.date else 0, m.uid), reverse=True)
        return msgs

    def messages(self, raw: str, limit=300) -> list[Msg]:
        def go(c):
            self._select(c, raw, fresh=True)
            typ, data = c.uid("SEARCH", None, "ALL")
            uids = data[0].split()
            if not uids:
                return []
            return self._fetch_headers(c, raw, b",".join(uids[-limit:]).decode())
        return self._run(go)

    def search(self, query: str, limit=300) -> list[Msg]:
        """Gmail search syntax (from:, has:attachment, label:, ...) over All Mail."""
        allmail = self.folder("all").raw

        def go(c):
            self._select(c, allmail, fresh=True)
            c.literal = query.encode()
            typ, data = c.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
            if typ != "OK":
                raise ImapError(f"search failed: {data}")
            uids = data[0].split()
            if not uids:
                return []
            return self._fetch_headers(c, allmail, b",".join(uids[-limit:]).decode())
        return self._run(go)

    def thread(self, thrids: list[int]) -> list[Msg]:
        """Every message of the given conversations (incl. sent replies), from All Mail, oldest first."""
        allmail = self.folder("all").raw
        thrids = [t for t in thrids if t]
        if not thrids:
            return []

        def go(c):
            self._select(c, allmail, fresh=True)
            terms = [f"X-GM-THRID {t}" for t in thrids]
            query = terms[-1]
            for t in reversed(terms[:-1]):
                query = f"OR {t} {query}"
            typ, data = c.uid("SEARCH", None, query)
            uids = data[0].split() if typ == "OK" else []
            if not uids:
                return []
            msgs = self._fetch_headers(c, allmail, b",".join(uids).decode())
            return sorted(msgs, key=lambda m: (m.date.timestamp() if m.date else 0, m.uid))
        return self._run(go)

    # -- drafts

    def append_draft(self, raw: bytes) -> int:
        """Save a message into Gmail's Drafts; returns its X-GM-MSGID."""
        drafts = self.folder("drafts").raw

        def go(c):
            typ, data = c.append(quote(drafts), "(\\Draft \\Seen)", None, raw)
            if typ != "OK":
                raise ImapError(f"draft save failed: {data}")
            m = re.search(rb"APPENDUID \d+ (\d+)", data[0] or b"")
            if not m:
                raise ImapError("draft saved but Gmail returned no id")
            self._select(c, drafts, fresh=True)
            typ, data = c.uid("FETCH", m.group(1).decode(), "(X-GM-MSGID)")
            for line in data:
                line = line[0] if isinstance(line, tuple) else line
                mm = re.search(rb"X-GM-MSGID (\d+)", line or b"")
                if mm:
                    return int(mm.group(1))
            raise ImapError("draft saved but not found")
        return self._run(go)

    def draft_versions(self, message_id: str) -> list[int]:
        """X-GM-MSGIDs of every copy of a draft in Gmail's Drafts (same Message-ID)."""
        drafts = self.folder("drafts").raw
        if not message_id:
            return []

        def go(c):
            self._select(c, drafts, fresh=True)
            typ, data = c.uid("SEARCH", None, "HEADER", "Message-ID", quote(message_id.strip("<>")))
            uids = data[0].split() if typ == "OK" else []
            if not uids:
                return []
            typ, data = c.uid("FETCH", b",".join(uids).decode(), "(X-GM-MSGID)")
            out = []
            for line in data:
                line = line[0] if isinstance(line, tuple) else line
                mm = re.search(rb"X-GM-MSGID (\d+)", line or b"")
                if mm:
                    out.append(int(mm.group(1)))
            return out
        return self._run(go)

    def delete_drafts(self, msgids: list[int]):
        if not msgids:
            return
        found = self.find(msgids, role="drafts")
        if not found:
            return
        raw = found[0].folder

        def go(c):
            self._select(c, raw, fresh=True)
            c.uid("STORE", ",".join(str(m.uid) for m in found), "+FLAGS", "(\\Deleted)")
            c.expunge()
            self.selected = None
        self._run(go)

    def fetch(self, msg: Msg) -> email.message.EmailMessage:
        return parse(self.fetch_raw(msg))

    def fetch_raw(self, msg: Msg) -> bytes:
        raw = self.fetch_raw_many(msg.folder, [msg.uid]).get(msg.uid)
        if raw is None:
            raise ImapError("message is gone")
        return raw

    def fetch_raw_many(self, folder: str, uids: list[int]) -> dict[int, bytes]:
        def go(c):
            self._select(c, folder)
            typ, data = c.uid("FETCH", ",".join(map(str, uids)), "(UID BODY.PEEK[])")
            out = {}
            for item in data:
                if isinstance(item, tuple):
                    m = re.search(rb"UID (\d+)", item[0])
                    if m:
                        out[int(m.group(1))] = item[1]
            return out
        return self._run(go)

    def unseen_all(self) -> dict[str, int]:
        """Unread count of every folder in one round trip (LIST-STATUS)."""
        def go(c):
            c.untagged_responses.pop("STATUS", None)
            typ, data = c._simple_command("LIST", '""', '"*"', "RETURN", "(STATUS", "(UNSEEN))")
            if typ != "OK":
                raise ImapError(f"list-status failed: {data}")
            typ, data = c._untagged_response(typ, data, "STATUS")
            out = {}
            for line in data or []:
                if not isinstance(line, bytes):
                    continue
                m = re.match(rb'("(?:[^"\\]|\\.)*"|\S+) \(UNSEEN (\d+)\)', line)
                if m:
                    name = m.group(1).decode()
                    out[_tokens(name)[0] if name.startswith('"') else name] = int(m.group(2))
            return out
        return self._run(go)

    # -- changes

    def set_seen(self, msgs: list[Msg], seen: bool):
        self.set_flag(msgs, "\\Seen", seen)

    def set_flag(self, msgs: list[Msg], flag: str, on: bool):
        for folder, uids in _by_folder(msgs):
            def go(c, folder=folder, uids=uids):
                self._select(c, folder)
                typ, data = c.uid("STORE", uids, "+FLAGS" if on else "-FLAGS", f"({flag})")
                if typ != "OK":
                    raise ImapError(f"flag change failed: {data}")
            self._run(go)
        for m in msgs:
            (m.flags.add if on else m.flags.discard)(flag)

    def change_labels(self, msgs: list[Msg], add=(), remove=()):
        """Add/remove Gmail labels.

        Always done from All Mail: inside a label's own folder Gmail hides that
        label (INBOX never reports \\Inbox) and silently ignores removing it.
        """
        self.change_labels_by_id([m.msgid for m in msgs], add, remove)

    def change_labels_by_id(self, msgids: list[int], add=(), remove=()):
        def fmt(labels):
            return "(" + " ".join(l if l.startswith("\\") else quote(utf7_encode(l)) for l in labels) + ")"

        found = self.find(msgids)
        if not found:
            raise ImapError("message not found in All Mail")
        uids = ",".join(str(m.uid) for m in found)
        allmail = found[0].folder

        def go(c):
            self._select(c, allmail)
            for op, labels in (("+X-GM-LABELS", add), ("-X-GM-LABELS", remove)):
                if labels:
                    typ, data = c.uid("STORE", uids, op, fmt(labels))
                    if typ != "OK":
                        raise ImapError(f"label change failed: {data}")
        self._run(go)
        return found

    def find(self, msgids: list[int], role="all") -> list[Msg]:
        """Messages by X-GM-MSGID in a special folder, one SEARCH for all of them."""
        raw = self.folder(role).raw
        if not msgids:
            return []

        def go(c):
            self._select(c, raw)
            terms = [f"X-GM-MSGID {mid}" for mid in msgids]
            query = terms[-1]
            for t in reversed(terms[:-1]):
                query = f"OR {t} {query}"
            typ, data = c.uid("SEARCH", None, query)
            if typ != "OK":
                raise ImapError(f"search failed: {data}")
            uids = [int(u) for u in data[0].split()]
            if len(uids) == len(msgids):
                typ, data = c.uid("FETCH", ",".join(map(str, uids)), "(UID X-GM-MSGID)")
                out = []
                for line in data:
                    line = line[0] if isinstance(line, tuple) else line
                    if not isinstance(line, bytes):
                        continue
                    mu, mm = re.search(rb"UID (\d+)", line), re.search(rb"X-GM-MSGID (\d+)", line)
                    if mu and mm:
                        out.append(Msg(folder=raw, uid=int(mu.group(1)), msgid=int(mm.group(1))))
                return out
            return [Msg(folder=raw, uid=u, msgid=0) for u in uids]
        return self._run(go)

    def by_msgid(self, msgids: list[int]) -> list[Msg]:
        """Full headers of messages by X-GM-MSGID, from All Mail."""
        found = self.find(msgids)
        if not found:
            return []
        allmail = found[0].folder

        def go(c):
            self._select(c, allmail)
            return self._fetch_headers(c, allmail, ",".join(str(m.uid) for m in found))
        return self._run(go)

    def trash(self, msgs: list[Msg]):
        bin_raw = self.folder("trash").raw
        for folder, uids in _by_folder(msgs):
            def go(c, folder=folder, uids=uids):
                self._select(c, folder)
                typ, data = c.uid("MOVE", uids, quote(bin_raw))
                if typ != "OK":
                    raise ImapError(f"trash failed: {data}")
            self._run(go)

    def untrash(self, msgids: list[int], to_raw: str):
        msgs = self.find(msgids, role="trash")
        for folder, uids in _by_folder(msgs):
            def go(c, folder=folder, uids=uids):
                self._select(c, folder)
                c.uid("MOVE", uids, quote(to_raw))
            self._run(go)

    # -- labels (categories)

    def create_label(self, name: str):
        def go(c):
            typ, data = c.create(quote(utf7_encode(name)))
            if typ != "OK" and b"exists" not in (data[0] or b"").lower():
                raise ImapError(f"cannot create label {name}: {data}")
        self._run(go)
        self._folders = None

    def rename_label(self, old: str, new: str):
        def go(c):
            if self.selected == utf7_encode(old):
                c.unselect() if hasattr(c, "unselect") else c.close()
                self.selected = None
            typ, data = c.rename(quote(utf7_encode(old)), quote(utf7_encode(new)))
            if typ != "OK":
                raise ImapError(f"cannot rename label: {data}")
        self._run(go)
        self._folders = None

    def delete_label(self, name: str):
        def go(c):
            if self.selected == utf7_encode(name):
                c.close()
                self.selected = None
            typ, data = c.delete(quote(utf7_encode(name)))
            if typ != "OK":
                raise ImapError(f"cannot delete label: {data}")
        self._run(go)
        self._folders = None


def parse(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _by_folder(msgs: list[Msg]):
    groups: dict[str, list[str]] = {}
    for m in msgs:
        groups.setdefault(m.folder, []).append(str(m.uid))
    return [(f, ",".join(u)) for f, u in groups.items()]
