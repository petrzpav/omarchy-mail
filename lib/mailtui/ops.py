"""Mail actions shared by the TUI and the CLI, each returning an undo record."""

import json
import time
from itertools import groupby

from . import jev, text
from .config import STATE_DIR, Config
from .imap import Folder, Mailbox, Msg

SORT_LOG = STATE_DIR / "sort.log"
SORT_SEEN = STATE_DIR / "seen"


def _current_labels(msg: Msg, view: Folder | None) -> set[str]:
    labels = set(msg.labels)
    if view and view.label and msg.folder == view.raw:
        labels.add(view.label)  # listed from that folder: Gmail hides the folder's own label
    return labels


def move(mbox: Mailbox, msgs: list[Msg], view: Folder | None, dst: str) -> dict:
    """File messages under category `dst`: add its label, drop the view's label and \\Inbox."""
    items = []
    for m in msgs:
        have = _current_labels(m, view)
        drop = {l for l in have if l == "\\Inbox" or (view and l == view.label)} - {dst}
        added = [dst] if dst not in have else []
        items.append({"msgid": m.msgid, "added": added, "removed": sorted(drop)})
        mbox.change_labels([m], add=added, remove=sorted(drop))
    return {"kind": "labels", "items": items, "what": f"Moved to {dst}"}


def archive(mbox: Mailbox, msgs: list[Msg], view: Folder | None) -> dict:
    items = []
    for m in msgs:
        drop = sorted({l for l in _current_labels(m, view) if l == "\\Inbox" or (view and l == view.label)})
        items.append({"msgid": m.msgid, "added": [], "removed": drop})
        mbox.change_labels([m], remove=drop)
    return {"kind": "labels", "items": items, "what": "Archived"}


def trash(mbox: Mailbox, msgs: list[Msg], view: Folder | None) -> dict:
    restore = view.raw if view and view.role in ("", "inbox") else mbox.folder("inbox").raw
    mbox.trash(msgs)
    return {"kind": "trash", "msgids": [m.msgid for m in msgs], "restore": restore, "what": "Moved to Bin"}


def star(mbox: Mailbox, full: list[Msg], on: bool) -> dict:
    """Star the given messages (the app passes a conversation's newest one), or unstar all flagged ones."""
    targets = full if on else [m for m in full if "\\Flagged" in m.flags]
    mbox.set_flag(targets, "\\Flagged", on)
    return {"kind": "star", "msgids": [m.msgid for m in targets], "on": on,
            "what": "Starred" if on else "Unstarred"}


def undo(mbox: Mailbox, rec: dict) -> str:
    if rec["kind"] == "star":
        mbox.set_flag(mbox.find(rec["msgids"]), "\\Flagged", not rec["on"])
    elif rec["kind"] == "trash":
        mbox.untrash(rec["msgids"], rec["restore"])
    else:
        key = lambda i: (tuple(i["added"]), tuple(i["removed"]))
        for (added, removed), group in groupby(sorted(rec["items"], key=key), key=key):
            mbox.change_labels_by_id([i["msgid"] for i in group], add=list(removed), remove=list(added))
    return "Undone: " + rec.get("what", "")


def set_read(mbox: Mailbox, msgs: list[Msg], seen: bool):
    mbox.set_seen(msgs, seen)


# ------------------------------------------------------------ Jev

def classify(cfg: Config, mbox: Mailbox, msg: Msg, categories=None) -> jev.Verdict:
    body = ""
    if msg.size < 1_500_000:
        body = text.body_text(mbox.fetch(msg))
    return jev.classify(cfg, f"{msg.sender} <{msg.sender_addr}>", msg.to, msg.subject, body, categories)


def _load_seen() -> set[int]:
    try:
        return {int(x) for x in SORT_SEEN.read_text().split()}
    except (FileNotFoundError, ValueError):
        return set()


def _log(entry: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with SORT_LOG.open("a") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def sort_inbox(cfg: Config, mbox: Mailbox, dry_run=False, limit=50, report=print) -> int:
    """Classify inbox mail Jev hasn't seen yet; move the confident ones. Returns moves made."""
    inbox = mbox.folder("inbox")
    seen = _load_seen()
    moved = 0
    todo = [m for m in mbox.messages(inbox.raw, limit=limit) if m.msgid not in seen]
    for m in reversed(todo):  # oldest first
        try:
            v = classify(cfg, mbox, m)
        except jev.JevError as e:
            report(f"  ! {m.subject[:60]}: {e}")
            continue
        target = cfg.category(v.choice)
        act = (target is not None and target.auto and v.choice != jev.KEEP
               and v.confidence >= cfg.jev_threshold)
        if act and target.keep_in_inbox:
            verb = "would label" if dry_run else "labelled"
        else:
            verb = ("would move" if dry_run else "moved") if act else "keep"
        report(f"  {verb:>10}  {v.choice:<14} {v.confidence:4.2f}  {m.sender[:22]:<22}  {m.subject[:60]}")
        if dry_run:
            continue
        entry = {"ts": time.time(), "msgid": m.msgid, "from": m.sender, "subject": m.subject,
                 "choice": v.choice, "confidence": v.confidence, "moved": act}
        if act and target.keep_in_inbox:
            added = [] if v.choice in m.labels else [v.choice]
            mbox.change_labels([m], add=added)
            rec = {"kind": "labels", "items": [{"msgid": m.msgid, "added": added, "removed": []}],
                   "what": f"Labelled {v.choice}"}
        elif act:
            rec = move(mbox, [m], inbox, v.choice)
        if act:
            entry["undo"] = rec
            moved += 1
        _log(entry)
        seen.add(m.msgid)
    if not dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SORT_SEEN.write_text("\n".join(str(s) for s in sorted(seen)[-5000:]) + "\n")
    return moved


RULES_SEEN = STATE_DIR / "rules-seen"


def apply_rules(cfg: Config, mbox: Mailbox, dry_run=False, days=3, report=print) -> int:
    """Per-address rules: confident junk goes to the Bin, confident important mail into the Inbox.
    Works on the address directly (Gmail filters may archive such mail before the Inbox sort sees it)."""
    from .imap import parse
    try:
        seen = {int(x) for x in RULES_SEEN.read_text().split()}
    except (FileNotFoundError, ValueError):
        seen = set()
    acted = 0
    for rule in cfg.rules:
        msgs = [m for m in mbox.search(f"to:{rule.to} newer_than:{days}d -in:trash -in:spam", limit=200)
                if m.msgid not in seen]
        for m in reversed(msgs):
            body = text.body_text(mbox.fetch(m)) if m.size < 1_000_000 else ""
            try:
                v = jev.ask(cfg, {"from": f"{m.sender} <{m.sender_addr}>", "subject": m.subject, "body": body[:2000]},
                            "Is this email important for the recipient, or junk that can go to the bin",
                            {"important": rule.important, "junk": rule.junk})
            except jev.JevError as e:
                report(f"  ! {m.subject[:60]}: {e}")
                continue
            if v.choice == "junk" and v.confidence >= rule.bin_threshold:
                act = "bin"
            elif v.choice == "important" and v.confidence >= cfg.jev_threshold and "\\Inbox" not in m.labels:
                act = "inbox"
            else:
                act = "keep"
            report(f"  {act:>6}  {v.choice:<9} {v.confidence:4.2f}  {m.sender[:22]:<22}  {m.subject[:55]}")
            if dry_run:
                continue
            entry = {"ts": time.time(), "msgid": m.msgid, "from": m.sender, "subject": m.subject,
                     "choice": "Bin" if act == "bin" else ("Inbox" if act == "inbox" else v.choice),
                     "confidence": v.confidence, "moved": act != "keep", "rule": rule.to}
            if act == "bin":
                entry["undo"] = trash(mbox, [m], None)
            elif act == "inbox":
                mbox.change_labels([m], add=["\\Inbox"])
                entry["undo"] = {"kind": "labels", "items": [{"msgid": m.msgid, "added": ["\\Inbox"], "removed": []}],
                                 "what": "Into Inbox"}
            acted += act != "keep"
            _log(entry)
            seen.add(m.msgid)
    if not dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RULES_SEEN.write_text("\n".join(str(s) for s in sorted(seen)[-5000:]) + "\n")
    return acted


def sort_history(limit=50) -> list[dict]:
    try:
        lines = SORT_LOG.read_text().splitlines()
    except FileNotFoundError:
        return []
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):     # entries logged before sender text was cleaned may hold escapes
            entries.append({k: text.clean_line(v) if isinstance(v, str) else v for k, v in e.items()})
    undone = {e["undone_of"] for e in entries if "undone_of" in e}
    out = []
    for e in reversed(entries):
        if e.get("moved") and e["msgid"] not in undone:
            out.append(e)
        if len(out) >= limit:
            break
    return out


def mark_undone(msgid: int):
    _log({"ts": time.time(), "msgid": msgid, "undone_of": msgid})


BACKFILL_LOG = STATE_DIR / "backfill.log"


def backfill(cfg: Config, mbox: Mailbox, days=180, workers=8, dry_run=False, report=print) -> dict:
    """Label older mail with Jev. Only adds labels: nothing leaves the Inbox or anywhere else."""
    from concurrent.futures import ThreadPoolExecutor
    from .imap import parse

    cats = {c.name for c in cfg.categories}
    msgs = mbox.search(f"newer_than:{days}d -in:sent -in:chats -in:spam -in:trash", limit=10000)
    todo = [m for m in msgs if not (m.labels & cats)]
    report(f"{len(msgs)} messages, {len(todo)} without a category")
    decided: dict[str, list[int]] = {}
    stats = {"labelled": 0, "unsure": 0, "errors": 0}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = BACKFILL_LOG.open("a")

    def ask(m, body):
        try:
            return m, jev.classify(cfg, f"{m.sender} <{m.sender_addr}>", m.to, m.subject, body)
        except jev.JevError as e:
            return m, e

    with ThreadPoolExecutor(workers) as pool:
        for start in range(0, len(todo), 40):
            chunk = todo[start:start + 40]
            small = [m for m in chunk if m.size < 1_000_000]
            raw = mbox.fetch_raw_many(chunk[0].folder, [m.uid for m in small]) if small else {}
            jobs = [pool.submit(ask, m, text.body_text(parse(raw[m.uid])) if m.uid in raw else "")
                    for m in chunk]
            for f in jobs:
                m, v = f.result()
                if isinstance(v, Exception):
                    stats["errors"] += 1
                    continue
                ok = v.choice in cats and v.confidence >= cfg.jev_threshold
                stats["labelled" if ok else "unsure"] += 1
                if ok:
                    decided.setdefault(v.choice, []).append(m.msgid)
                log.write(json.dumps({"msgid": m.msgid, "subject": m.subject, "from": m.sender_addr,
                                      "choice": v.choice, "confidence": v.confidence, "applied": ok and not dry_run},
                                     ensure_ascii=False) + "\n")
            report(f"  {min(start + 40, len(todo))}/{len(todo)}  " +
                   "  ".join(f"{k} {len(v)}" for k, v in sorted(decided.items())))
    log.close()
    if not dry_run:
        for label, ids in decided.items():
            for i in range(0, len(ids), 100):
                mbox.change_labels_by_id(ids[i:i + 100], add=[label])
    stats["by_label"] = {k: len(v) for k, v in decided.items()}
    return stats
