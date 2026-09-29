"""Local-first access to the mailbox.

The UI never waits for Gmail when it doesn't have to: folder lists, message
lists and bodies are cached on disk and shown at once, while two IMAP
connections work behind it -- `fg` for what the user just did (open, move,
archive), `bg` for syncing, unread counts and prefetching bodies. Each runs
its jobs strictly in order on its own thread, so a slow sync never delays an
action and actions reach Gmail in the order they were made.
"""

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .config import CACHE_DIR, STATE_DIR, Config
from .imap import Folder, Mailbox, Msg

STATE_LOG = STATE_DIR / "errors.log"
BODY_LIMIT = 600_000      # don't prefetch bodies bigger than this
BODY_KEEP = 3000          # bodies kept on disk


def msg_to_dict(m: Msg) -> dict:
    d = asdict(m)
    d["flags"], d["labels"] = sorted(m.flags), sorted(m.labels)
    d["date"] = m.date.isoformat() if m.date else None
    return d


def msg_from_dict(d: dict) -> Msg:
    d = dict(d)
    d["flags"], d["labels"] = set(d.get("flags", [])), set(d.get("labels", []))
    d["date"] = datetime.fromisoformat(d["date"]) if d.get("date") else None
    return Msg(**d)


class Store:
    def __init__(self, cfg: Config, call_ui):
        self.cfg = cfg
        self.call_ui = call_ui              # schedules a callable on the UI thread
        self.fg, self.bg = Mailbox(cfg), Mailbox(cfg)
        self._pools = {"fg": ThreadPoolExecutor(1, "mail-fg"), "bg": ThreadPoolExecutor(1, "mail-bg"),
                       "drafts": ThreadPoolExecutor(1, "mail-drafts")}
        self.drafts_mbox = Mailbox(cfg)      # drafts sync never waits behind prefetching
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
        for p in self._pools.values():
            p.shutdown(wait=False, cancel_futures=True)
        self.fg.close()
        self.bg.close()
        self.drafts_mbox.close()

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
            (self.dir / "unread").write_text(f"{counts.get(inbox.raw, 0)}\n")

    # -- message list cache

    def _list_path(self, raw: str) -> Path:
        return self.dir / "lists" / (hashlib.sha1(raw.encode()).hexdigest()[:16] + ".json")

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

    def prefetch_threads(self, thrids: list[int], mbox=None, limit_bodies=40) -> int:
        """Background: download whole threads not cached yet (one search), then their bodies."""
        mbox = mbox or self.bg
        want = [t for t in thrids if t and self.cached_thread(t) is None]
        msgs = mbox.thread(want) if want else []
        for t in want:
            self.save_thread(t, [m for m in msgs if m.thrid == t])
        every = [m for t in thrids if t for m in (self.cached_thread(t) or [])]
        return self.prefetch(every[-limit_bodies:]) if every else 0

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

    def prefetch(self, msgs: list[Msg]):
        """Background: download bodies we don't have yet, in one IMAP round trip per folder."""
        want = [m for m in msgs if m.size < BODY_LIMIT and not self._body_path(m).exists()]
        if not want:
            return 0
        got = 0
        by_folder: dict[str, list[Msg]] = {}
        for m in want:
            by_folder.setdefault(m.folder, []).append(m)
        for folder, ms in by_folder.items():
            raw = self.bg.fetch_raw_many(folder, [m.uid for m in ms])
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
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(path)
