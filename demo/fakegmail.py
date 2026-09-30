"""An in-memory Gmail for the demo: the same interface as imap.Mailbox, no network.

Every Mailbox the app opens shares one set of messages, so a label Jev adds from
the background lane shows up in the list at once. Messages are real RFC 822 bytes,
labels and flags behave like Gmail's (a folder's own label is hidden inside it).
"""

import email
import email.policy
import threading
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from mailtui.imap import Folder, ImapError, Msg, decode, parse

SYSTEM = [("INBOX", "inbox", "\\Inbox"), ("[Gmail]/All Mail", "all", None),
          ("[Gmail]/Sent Mail", "sent", "\\Sent"), ("[Gmail]/Drafts", "drafts", "\\Draft"),
          ("[Gmail]/Starred", "starred", None), ("[Gmail]/Bin", "trash", None)]


class World:
    """The mailbox contents: msgid -> {raw, labels, flags, thrid, trashed}."""

    def __init__(self):
        self.lock = threading.RLock()
        self.msgs: dict[int, dict] = {}
        self.labels: list[str] = []
        self.next_id = 1000

    def add(self, raw: bytes, labels=(), flags=(), thrid: int = 0) -> int:
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            self.msgs[mid] = {"raw": raw, "labels": set(labels), "flags": set(flags),
                              "thrid": thrid or mid, "trashed": False}
            return mid


WORLD = World()


class FakeMailbox:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = WORLD.lock

    def close(self):
        pass

    # -- folders

    def folders(self, refresh=False) -> list[Folder]:
        out = [Folder("Inbox" if role == "inbox" else raw.removeprefix("[Gmail]/"), raw, role)
               for raw, role, _ in SYSTEM]
        return out + [Folder(name, name, "") for name in WORLD.labels]

    def folder(self, role: str) -> Folder:
        f = next((f for f in self.folders() if f.role == role), None)
        if not f:
            raise ImapError(f"no {role} folder")
        return f

    def label_folder(self, name: str) -> Folder | None:
        return next((f for f in self.folders() if not f.role and f.name == name), None)

    def _in(self, raw: str, rec: dict) -> bool:
        role = next((r for n, r, _ in SYSTEM if n == raw), "")
        if role == "trash":
            return rec["trashed"]
        if rec["trashed"]:
            return False
        if role == "all":
            return "\\Draft" not in rec["labels"]
        if role == "starred":
            return "\\Flagged" in rec["flags"]
        label = next((l for n, _, l in SYSTEM if n == raw), None) or raw
        return label in rec["labels"]

    def _msg(self, raw: str, mid: int) -> Msg:
        rec = WORLD.msgs[mid]
        h = email.message_from_bytes(rec["raw"])
        own = next((l for n, _, l in SYSTEM if n == raw), raw)
        m = Msg(folder=raw, uid=mid, msgid=mid, thrid=rec["thrid"], flags=set(rec["flags"]),
                labels={l for l in rec["labels"] if l != own}, size=len(rec["raw"]))
        name, addr = parseaddr(decode(h["From"]))
        m.sender, m.sender_addr = name or addr, addr
        m.subject = decode(h["Subject"]) or "(no subject)"
        m.to, m.cc = decode(h["To"]), decode(h["Cc"])
        m.message_id = (h["Message-ID"] or "").strip()
        m.date = parsedate_to_datetime(h["Date"]) if h["Date"] else None
        m.recipients = [a.lower() for _, a in getaddresses([decode(h[k]) for k in ("To", "Cc") if h[k]]) if a]
        return m

    def _list(self, raw: str, ids) -> list[Msg]:
        msgs = [self._msg(raw, i) for i in ids]
        return sorted(msgs, key=lambda m: (m.date.timestamp() if m.date else 0, m.uid), reverse=True)

    def unseen(self, raw: str) -> int:
        with self.lock:
            return sum(1 for r in WORLD.msgs.values() if self._in(raw, r) and "\\Seen" not in r["flags"])

    def unseen_all(self) -> dict[str, int]:
        return {f.raw: self.unseen(f.raw) for f in self.folders()}

    # -- listing

    def messages(self, raw: str, limit=300) -> list[Msg]:
        with self.lock:
            return self._list(raw, [i for i, r in WORLD.msgs.items() if self._in(raw, r)])[:limit]

    def search(self, query: str, limit=300) -> list[Msg]:
        q = query.lower()
        with self.lock:
            ids = [i for i, r in WORLD.msgs.items()
                   if not r["trashed"] and q in r["raw"].decode(errors="replace").lower()]
            return self._list("[Gmail]/All Mail", ids)[:limit]

    def thread(self, thrids: list[int]) -> list[Msg]:
        with self.lock:
            ids = [i for i, r in WORLD.msgs.items()
                   if r["thrid"] in thrids and not r["trashed"] and "\\Draft" not in r["labels"]]
            return list(reversed(self._list("[Gmail]/All Mail", ids)))

    def fetch(self, msg: Msg) -> email.message.EmailMessage:
        return parse(self.fetch_raw(msg))

    def fetch_raw(self, msg: Msg) -> bytes:
        if msg.msgid not in WORLD.msgs:
            raise ImapError("message is gone")
        return WORLD.msgs[msg.msgid]["raw"]

    def fetch_raw_many(self, folder: str, uids: list[int]) -> dict[int, bytes]:
        return {u: WORLD.msgs[u]["raw"] for u in uids if u in WORLD.msgs}

    # -- drafts

    def append_draft(self, raw: bytes) -> int:
        return WORLD.add(raw, labels={"\\Draft"}, flags={"\\Seen", "\\Draft"})

    def draft_versions(self, message_id: str) -> list[int]:
        with self.lock:
            return [i for i, r in WORLD.msgs.items() if "\\Draft" in r["labels"] and message_id
                    and (email.message_from_bytes(r["raw"])["Message-ID"] or "").strip() == message_id.strip()]

    def delete_drafts(self, msgids: list[int]):
        with self.lock:
            for i in msgids:
                if i in WORLD.msgs and "\\Draft" in WORLD.msgs[i]["labels"]:
                    del WORLD.msgs[i]

    # -- changes

    def set_seen(self, msgs: list[Msg], seen: bool):
        self.set_flag(msgs, "\\Seen", seen)

    def set_flag(self, msgs: list[Msg], flag: str, on: bool):
        with self.lock:
            for m in msgs:
                if m.msgid in WORLD.msgs:
                    (WORLD.msgs[m.msgid]["flags"].add if on else WORLD.msgs[m.msgid]["flags"].discard)(flag)
                (m.flags.add if on else m.flags.discard)(flag)

    def change_labels(self, msgs: list[Msg], add=(), remove=()):
        return self.change_labels_by_id([m.msgid for m in msgs], add, remove)

    def change_labels_by_id(self, msgids: list[int], add=(), remove=()):
        with self.lock:
            for i in msgids:
                rec = WORLD.msgs.get(i)
                if rec:
                    rec["labels"] |= set(add)
                    rec["labels"] -= set(remove)
            return self.find(msgids)

    def find(self, msgids: list[int], role="all") -> list[Msg]:
        raw = self.folder(role).raw
        with self.lock:
            return [Msg(folder=raw, uid=i, msgid=i) for i in msgids
                    if i in WORLD.msgs and self._in(raw, WORLD.msgs[i])]

    def trash(self, msgs: list[Msg]):
        with self.lock:
            for m in msgs:
                if m.msgid in WORLD.msgs:
                    WORLD.msgs[m.msgid]["trashed"] = True

    def untrash(self, msgids: list[int], to_raw: str):
        with self.lock:
            for i in msgids:
                if i in WORLD.msgs:
                    WORLD.msgs[i]["trashed"] = False

    # -- labels

    def create_label(self, name: str):
        if name not in WORLD.labels:
            WORLD.labels.append(name)

    def rename_label(self, old: str, new: str):
        WORLD.labels[WORLD.labels.index(old)] = new
        for r in WORLD.msgs.values():
            if old in r["labels"]:
                r["labels"] = (r["labels"] - {old}) | {new}

    def delete_label(self, name: str):
        WORLD.labels.remove(name)
        for r in WORLD.msgs.values():
            r["labels"].discard(name)
