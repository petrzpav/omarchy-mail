"""Local-first access to the mailbox.

The UI never waits for Gmail when it doesn't have to: folder lists, message
lists and bodies are cached on disk and shown at once, while IMAP connections
work behind it -- `fg` for what the user just did (open, move, archive),
`sync` for message lists and unread counts, `bg` for prefetching bodies and
`sort` for Jev. Each runs its jobs strictly in order on its own thread, so a
slow download never delays a sync or an action, and actions reach Gmail in the
order they were made. A `Watcher` idles in INBOX and reports changes at once.

Every change, from the client or the `mail` command, is also written into the
cached lists right away (`reflect`) and announced in CACHE_DIR/changed, so
whatever reads the cache next (the client, a restart) shows it at once.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from email.utils import getaddresses
from pathlib import Path

from . import text
from .config import CACHE_DIR, STATE_DIR, Config
from .imap import Folder, Mailbox, Msg, Watcher

STATE_LOG = STATE_DIR / "errors.log"
BODY_LIMIT = 600_000      # don't prefetch bodies bigger than this
BODY_KEEP = 3000          # bodies kept on disk
CHANGED = CACHE_DIR / "changed"       # "<pid> <what>", rewritten after every change to the cache
SORT_UNIT = "mail-sort.service"       # started on new mail when the user enabled the sort timer
ROBOT = re.compile(r"no-?reply|do-?not-?reply|unsubscribe|mailer-daemon|bounce|^notifications?@", re.I)


def msg_to_dict(m: Msg) -> dict:
    d = asdict(m)
    d["flags"], d["labels"] = sorted(m.flags), sorted(m.labels)
    d["date"] = m.date.isoformat() if m.date else None
    return d


# Sender text a cached message carries. Caches written before it was cleaned at the source
# still hold escape sequences, so it is cleaned again on every load.
SENDER_FIELDS = ("sender", "sender_addr", "subject", "to", "cc", "message_id")


def clean_dict(d: dict) -> dict:
    """A cached message, its sender text terminal-safe and on one line."""
    d = dict(d)
    for k in SENDER_FIELDS:
        if isinstance(d.get(k), str):
            d[k] = text.clean_line(d[k])
    if isinstance(d.get("recipients"), list):
        d["recipients"] = [text.clean_line(a) for a in d["recipients"] if isinstance(a, str)]
    return d


def msg_from_dict(d: dict) -> Msg:
    d = clean_dict(d)
    d["flags"], d["labels"] = set(d.get("flags", [])), set(d.get("labels", []))
    d["date"] = datetime.fromisoformat(d["date"]) if d.get("date") else None
    return Msg(**d)


# ------------------------------------------------------------ the cache on disk, shared with the CLI

def write_json(path: Path, data):
    """Atomic, and safe with another process writing the same file (a unique temp name)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, ensure_ascii=False))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def list_path(raw: str) -> Path:
    return CACHE_DIR / "lists" / (hashlib.sha1(raw.encode()).hexdigest()[:16] + ".json")


def cached_folders() -> list[Folder]:
    try:
        return [Folder(**f) for f in json.loads((CACHE_DIR / "folders.json").read_text())["folders"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save_unread(n: int):
    """The bar's count. Rewritten in place (not replaced) so the bar's file watch keeps working."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "unread").write_text(f"{n}\n")


def announce(what: str = ""):
    """Tell a running client (and the next start) that the cache changed under it."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CHANGED.write_text(f"{os.getpid()} {what}\n")
    except OSError:
        pass


def changed_by() -> tuple[float, int]:
    """(mtime, pid) of the last announce(), (0, 0) if none."""
    try:
        st = CHANGED.stat()
        return st.st_mtime, int(CHANGED.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0, 0


def inverse(rec: dict) -> dict:
    """The cache change an undo of `rec` makes (what ops.undo does in Gmail)."""
    if rec["kind"] == "labels":
        return {"kind": "labels", "items": [{"msgid": i["msgid"], "added": i["removed"], "removed": i["added"]}
                                            for i in rec["items"]]}
    if rec["kind"] in ("star", "seen"):
        return {**rec, "on": not rec["on"]}
    return {"kind": "untrash", "msgids": rec.get("msgids", [])}


_reflect_lock = threading.Lock()


def reflect(rec: dict, msgs=(), folders: list[Folder] | None = None, what: str = ""):
    """Apply a change to every cached list and conversation at once, before any sync.

    `rec` is an ops undo record ("labels", "trash", "star") or {"kind": "seen", "msgids", "on"}.
    Labels removed take the message out of those folders' lists; labels added put it into
    theirs (when `msgs` has it); flags change everywhere. Then announce() tells the client.
    """
    kind = rec.get("kind")
    if kind == "labels":
        items = {i["msgid"]: (set(i["added"]), set(i["removed"])) for i in rec.get("items", [])}
    else:
        items = {mid: None for mid in rec.get("msgids", [])}
    if not items or kind not in ("labels", "trash", "star", "seen", "untrash"):
        return
    known = {f.raw: f for f in cached_folders()}
    known.update({f.raw: f for f in folders or []})
    folders = list(known.values())
    by_path = {list_path(f.raw): f for f in folders}
    flag = {"star": "\\Flagged", "seen": "\\Seen"}.get(kind)
    needles = [f'"msgid": {mid},' for mid in items]
    lists = CACHE_DIR / "lists"
    with _reflect_lock, open(_lock_file(), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in lists.glob("*.json") if kind != "untrash" else []:
            try:
                raw = path.read_text()
                if not any(n in raw for n in needles):
                    continue
                data = json.loads(raw)
            except (OSError, ValueError):
                continue
            if not isinstance(data, list):
                continue
            f = by_path.get(path)            # None: a conversation or something unknown
            out = []
            for d in data:
                mid = d.get("msgid") if isinstance(d, dict) else None
                if mid not in items:
                    out.append(d)
                    continue
                d = dict(d)
                if kind == "trash":
                    if f is not None and f.role == "trash":
                        out.append(d)
                    continue
                if kind == "labels":
                    added, removed = items[mid]
                    if f is not None and f.label in removed:
                        continue                 # it left this folder
                    own = f.label if f else None
                    d["labels"] = sorted((set(d.get("labels") or []) | (added - {own})) - removed)
                else:
                    fl = set(d.get("flags") or [])
                    (fl.add if rec["on"] else fl.discard)(flag)
                    d["flags"] = sorted(fl)
                    if kind == "star" and not rec["on"] and f is not None and f.role == "starred" \
                            and "\\Flagged" not in fl:
                        continue
                out.append(d)
            write_json(path, out)
        if kind == "labels" and msgs:
            _add_to_lists(items, {m.msgid: m for m in msgs}, folders)
    announce(what or kind)


def _add_to_lists(items: dict, msgs: dict[int, Msg], folders: list[Folder]):
    """Put messages into the cached lists of the labels they just got (only lists already cached)."""
    by_label = {f.label: f for f in folders if f.label}
    for label in {l for added, _ in items.values() for l in added}:
        f = by_label.get(label)
        path = list_path(f.raw) if f else None
        if not path or not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        have = {d.get("msgid") for d in data if isinstance(d, dict)}
        new = []
        for mid, (added, removed) in items.items():
            m = msgs.get(mid)
            if label in added and m and mid not in have:
                d = msg_to_dict(m)
                d["labels"] = sorted((set(d["labels"]) | added) - removed - {label})
                new.append(d)
        if new:
            data = sorted(data + new, key=_date_key, reverse=True)
            write_json(path, data)


def _date_key(d: dict):
    try:
        return datetime.fromisoformat(d["date"]).timestamp(), d.get("uid", 0)
    except (KeyError, TypeError, ValueError):
        return 0, d.get("uid", 0) if isinstance(d, dict) else 0


def _lock_file() -> Path:
    (CACHE_DIR / "lists").mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / ".lock"


def drop_thread(thrid: int):
    """Forget a cached conversation, so the next open fetches it whole (e.g. after replying)."""
    if thrid:
        list_path(f"thread:{thrid}").unlink(missing_ok=True)


def start_sort():
    """Let the sort service file new mail now, if the user turned the sort timer on. systemd runs
    one sort at a time, so starting it while one runs is harmless."""
    if not SORT_UNIT:
        return
    timer = SORT_UNIT.removesuffix(".service") + ".timer"

    def go():
        try:
            if subprocess.run(["systemctl", "--user", "is-active", "-q", timer], timeout=5).returncode == 0:
                subprocess.run(["systemctl", "--user", "start", "--no-block", SORT_UNIT], timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    threading.Thread(target=go, name="mail-sort-kick", daemon=True).start()


class Store:
    FULL_EVERY = 300     # a folder whose STATUS didn't change is still re-read this often

    def __init__(self, cfg: Config, call_ui):
        self.cfg = cfg
        self.call_ui = call_ui              # schedules a callable on the UI thread
        self.fg, self.bg, self.sync = Mailbox(cfg), Mailbox(cfg), Mailbox(cfg)
        self._pools = {"fg": ThreadPoolExecutor(1, "mail-fg"), "bg": ThreadPoolExecutor(1, "mail-bg"),
                       "sync": ThreadPoolExecutor(1, "mail-sync"), "sort": ThreadPoolExecutor(1, "mail-sort"),
                       "drafts": ThreadPoolExecutor(1, "mail-drafts")}
        self.drafts_mbox = Mailbox(cfg)      # drafts sync never waits behind prefetching
        self.sort_mbox = Mailbox(cfg)        # Jev's sort never holds up a sync
        self.watcher: Watcher | None = None
        self.status: dict[str, tuple[tuple, float]] = {}   # raw -> (STATUS when last read in full, when)
        self._latest: dict[str, int] = {}
        self._latest_lock = threading.Lock()
        self.dir = CACHE_DIR
        (self.dir / "lists").mkdir(parents=True, exist_ok=True)
        (self.dir / "bodies").mkdir(parents=True, exist_ok=True)

    # -- jobs

    def submit(self, lane: str, fn, done=None, error=None, latest: str | None = None):
        """Run fn on the lane's thread. With `latest`, a newer job of the same key
        makes this one a no-op if it hasn't started yet (sidebar scrolling etc.)."""
        gen = None
        if latest:
            with self._latest_lock:
                gen = self._latest[latest] = self._latest.get(latest, 0) + 1

        def job():
            if latest and self._latest.get(latest) != gen:
                return
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001 - reported to the UI, always logged
                import traceback
                try:
                    STATE_LOG.parent.mkdir(parents=True, exist_ok=True)
                    with STATE_LOG.open("a") as f:
                        f.write(f"--- {time.strftime('%F %T')} {lane}\n{traceback.format_exc()}\n")
                except OSError:
                    pass
                if error:
                    self.call_ui(error, e)
                return
            if done:
                self.call_ui(done, res)
        self._pools[lane].submit(job)

    def close(self):
        if self.watcher:
            self.watcher.stop()
        for p in self._pools.values():
            p.shutdown(wait=False, cancel_futures=True)
        for mbox in self._mailboxes():
            mbox.close()

    def _mailboxes(self):
        return (self.fg, self.bg, self.sync, self.drafts_mbox, self.sort_mbox)

    # -- push

    def watch(self, on_change):
        """Start the INBOX watcher; on_change(kind) runs on the UI thread."""
        self.watcher = Watcher(self.cfg, lambda kind: self.call_ui(on_change, kind), self.wake)
        self.watcher.start()

    def watching(self) -> bool:
        return bool(self.watcher and self.watcher.healthy)

    def wake(self):
        """After a suspend every connection is dead: drop them, so nothing waits 30 s on one."""
        for mbox in self._mailboxes():
            mbox.drop()

    def messages(self, raw: str, limit=300, force=False) -> list[Msg] | None:
        """A folder's list from Gmail, or None when its STATUS says nothing changed since the
        last full read (one round trip instead of a whole list)."""
        st = self.sync.status(raw)
        prev = self.status.get(raw)
        if not force and st and prev and prev[0] == st and time.time() - prev[1] < self.FULL_EVERY:
            return None
        msgs = self.sync.messages(raw, limit=limit)
        if st:
            self.status[raw] = (st, time.time())
        return msgs

    # -- folders + counts cache

    def cached_folders(self) -> tuple[list[Folder], dict[str, int]]:
        try:
            d = json.loads((self.dir / "folders.json").read_text())
            return [Folder(**f) for f in d["folders"]], d["counts"]
        except (OSError, ValueError, KeyError, TypeError):
            return [], {}

    def save_folders(self, folders: list[Folder], counts: dict[str, int]):
        self._write(self.dir / "folders.json",
                    {"folders": [asdict(f) for f in folders], "counts": counts})
        inbox = next((f for f in folders if f.role == "inbox"), None)
        if inbox:
            save_unread(counts.get(inbox.raw, 0))

    # -- message list cache

    def _list_path(self, raw: str) -> Path:
        return list_path(raw)

    def cached_list(self, raw: str) -> list[Msg] | None:
        try:
            return [msg_from_dict(d) for d in json.loads(self._list_path(raw).read_text())]
        except (OSError, ValueError, TypeError):
            return None

    def save_list(self, raw: str, msgs: list[Msg]):
        self._write(self._list_path(raw), [msg_to_dict(m) for m in msgs])

    # -- whole conversations (incl. sent replies), so a thread opens complete in one paint

    def cached_thread(self, thrid: int) -> list[Msg] | None:
        return self.cached_list(f"thread:{thrid}") if thrid else None

    def save_thread(self, thrid: int, msgs: list[Msg]):
        if thrid and msgs:
            self.save_list(f"thread:{thrid}", msgs)

    def prefetch_threads(self, thrids: list[int], mbox=None, limit_bodies=40, listed=()) -> int:
        """Background: download whole threads not cached yet, or missing a message the list
        shows (a new reply), in one search; then their bodies."""
        mbox = mbox or self.bg
        listed_ids: dict[int, set[int]] = {}
        for m in listed:
            listed_ids.setdefault(m.thrid, set()).add(m.msgid)
        want = []
        for t in thrids:
            cached = self.cached_thread(t) if t else None
            if t and (cached is None or listed_ids.get(t, set()) - {m.msgid for m in cached}):
                want.append(t)
        msgs = mbox.thread(want) if want else []
        for t in want:
            self.save_thread(t, [m for m in msgs if m.thrid == t])
        every = [m for t in thrids if t for m in (self.cached_thread(t) or [])]
        return self.prefetch(every[-limit_bodies:]) if every else 0

    # -- address book, from the cached message lists

    def contacts(self) -> list[tuple[str, str]]:
        """(name, address) of everyone in the cached mail, people I wrote to first, then by
        how often and how recently they appear. Rebuilt at most once a minute."""
        if time.time() - getattr(self, "_contacts_at", 0) < 60:
            return self._contacts
        me = {i.email.lower() for i in self.cfg.identities} | {self.cfg.email.lower()}
        score: dict[str, float] = {}
        names: dict[str, tuple[str, str]] = {}       # addr -> (date, name) of the latest mention
        seen: set[int] = set()

        def add(name, addr, weight, date):
            addr = addr.strip().lower()
            if not addr or "@" not in addr or addr in me or ROBOT.search(addr):
                return
            score[addr] = score.get(addr, 0) + weight
            if name and name != addr and date >= names.get(addr, ("", ""))[0]:
                names[addr] = (date, name.strip().strip('"'))
            elif addr not in names:
                names[addr] = ("", "")
        for path in (self.dir / "lists").glob("*.json"):
            try:
                msgs = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                m = clean_dict(m)
                if m.get("msgid") in seen:
                    continue
                seen.add(m.get("msgid"))
                date = m.get("date") or ""
                if (m.get("sender_addr") or "").lower() in me:
                    for n, a in getaddresses([m.get("to") or "", m.get("cc") or ""]):
                        add(n, a, 5, date)
                else:
                    add(m.get("sender") or "", m.get("sender_addr") or "", 1, date)
                    for n, a in getaddresses([m.get("cc") or ""]):
                        add(n, a, 0.5, date)
        order = sorted(score, key=lambda a: (score[a], names[a][0]), reverse=True)
        self._contacts = [(names[a][1], a) for a in order]
        self._contacts_at = time.time()
        return self._contacts

    # -- bodies cache

    def _body_path(self, m: Msg) -> Path:
        return self.dir / "bodies" / f"{m.msgid}.eml"

    def cached_body(self, m: Msg) -> bytes | None:
        try:
            return self._body_path(m).read_bytes()
        except OSError:
            return None

    def save_body(self, m: Msg, raw: bytes):
        self._body_path(m).write_bytes(raw)

    def prefetch(self, msgs: list[Msg], mbox=None):
        """Background: download bodies we don't have yet, in one IMAP round trip per folder."""
        mbox = mbox or self.bg
        want = [m for m in msgs if m.size < BODY_LIMIT and not self._body_path(m).exists()]
        if not want:
            return 0
        got = 0
        by_folder: dict[str, list[Msg]] = {}
        for m in want:
            by_folder.setdefault(m.folder, []).append(m)
        for folder, ms in by_folder.items():
            raw = mbox.fetch_raw_many(folder, [m.uid for m in ms])
            for m in ms:
                if m.uid in raw:
                    self.save_body(m, raw[m.uid])
                    got += 1
        return got

    def prune_bodies(self):
        files = sorted((self.dir / "bodies").glob("*.eml"), key=lambda p: p.stat().st_mtime)
        for p in files[:-BODY_KEEP]:
            p.unlink(missing_ok=True)

    @staticmethod
    def _write(path: Path, data):
        write_json(path, data)
