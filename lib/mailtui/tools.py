"""Scriptable mail commands (for shell scripts and AI agents): list, read, file and draft
without opening the client. Ids are Gmail's X-GM-MSGID / X-GM-THRID numbers; a thread id
is the id of its first message, so either kind of id names a conversation.

Reading prints compact text (or JSON with --json); every change acts on whole conversations,
like the client, and saves an undo record that `mail undo` reverts.
"""

import json
import mimetypes
import sys
from email.utils import getaddresses
from pathlib import Path

from . import drafts, ops, smtp, text
from .text import formataddr
from .config import STATE_DIR, Config
from .imap import Folder, ImapError, Mailbox, Msg

UNDO_FILE = STATE_DIR / "cli-undo.json"
ROLES = ("inbox", "all", "sent", "trash", "spam", "drafts", "starred", "important")


def _die(msg: str):
    sys.exit(f"mail: {msg}")


def _date(m: Msg) -> str:
    return m.date.astimezone().strftime("%Y-%m-%d %H:%M") if m.date else ""


def _labels(m: Msg) -> list[str]:
    return sorted(l.lstrip("\\") for l in m.labels if l not in ("\\Important",))


def _row(m: Msg, count: int = 1, unread: bool = False) -> dict:
    return {"thread": m.thrid or m.msgid, "id": m.msgid, "date": _date(m),
            "from": f"{m.sender} <{m.sender_addr}>" if m.sender != m.sender_addr else m.sender_addr,
            "subject": m.subject, "labels": _labels(m), "unread": unread, "messages": count,
            "starred": "\\Flagged" in m.flags}


def _print_rows(rows: list[dict], as_json: bool):
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        flags = ("*" if r["unread"] else " ") + ("★" if r["starred"] else " ")
        n = f" ({r['messages']})" if r["messages"] > 1 else ""
        labels = f"  [{', '.join(r['labels'])}]" if r["labels"] else ""
        print(f"{r['thread']}  {flags} {r['date']}  {r['from'][:40]:<40}  {r['subject']}{n}{labels}")
    if not rows:
        print("(nothing)")


def _conversations(msgs: list[Msg], limit: int) -> list[dict]:
    """Newest first, one row per conversation (its newest message)."""
    by: dict[int, list[Msg]] = {}
    for m in msgs:
        by.setdefault(m.thrid or m.msgid, []).append(m)
    rows = []
    for ms in by.values():
        newest = max(ms, key=lambda m: (m.date.timestamp() if m.date else 0, m.uid))
        rows.append((newest, len(ms), any(not m.seen for m in ms)))
    rows.sort(key=lambda r: r[0].date.timestamp() if r[0].date else 0, reverse=True)
    return [_row(m, n, u) for m, n, u in rows[:limit]]


def resolve_folder(mbox: Mailbox, name: str) -> Folder:
    low = name.lower()
    if low in ROLES:
        return mbox.folder(low)
    for f in mbox.folders():
        if f.name.lower() == low or f.raw == name:
            return f
    _die(f"no folder or category called {name!r}; try `mail folders`")


def thread(mbox: Mailbox, ident: int) -> list[Msg]:
    """Every message of the conversation named by a thread id or any message id in it."""
    msgs = mbox.thread([ident])
    if not msgs:
        heads = mbox.by_msgid([ident])
        if heads:
            msgs = mbox.thread([heads[0].thrid]) or heads
    if not msgs:
        _die(f"no message or thread {ident}")
    return msgs


# ------------------------------------------------------------ reading

def cmd_folders(cfg: Config, args):
    mbox = Mailbox(cfg)
    folders = mbox.folders()        # before LIST-STATUS, whose untagged LIST lines a later LIST would repeat
    counts = mbox.unseen_all()
    cats = {c.name: c for c in cfg.categories}
    rows = []
    for f in folders:
        c = cats.get(f.name)
        rows.append({"name": f.name, "role": f.role or ("category" if c else "label"),
                     "unread": counts.get(f.raw, 0), "description": c.description if c else ""})
    mbox.close()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        for r in rows:
            print(f"{r['name']:<24} {r['role']:<9} {r['unread']:>5} unread  {r['description']}")


def cmd_ls(cfg: Config, args):
    mbox = Mailbox(cfg)
    f = resolve_folder(mbox, args.folder)
    msgs = mbox.messages(f.raw, limit=max(args.limit * 3, 60))
    if args.unread:
        msgs = [m for m in msgs if not m.seen]
    mbox.close()
    _print_rows(_conversations(msgs, args.limit), args.json)


def cmd_search(cfg: Config, args):
    mbox = Mailbox(cfg)
    msgs = mbox.search(args.query, limit=max(args.limit * 3, 60))
    mbox.close()
    _print_rows(_conversations(msgs, args.limit), args.json)


def _message(mbox: Mailbox, m: Msg, full: bool, max_chars: int) -> dict:
    parsed = mbox.fetch(m)
    body, _ = text.body(parsed)
    quoted = ""
    if not full:
        body, quoted = text.split_quoted(body)
    if max_chars and len(body) > max_chars:
        body = body[:max_chars] + f"\n… [{len(body) - max_chars} more characters, use --max 0]"
    return {"id": m.msgid, "date": _date(m), "from": f"{m.sender} <{m.sender_addr}>" if m.sender != m.sender_addr else m.sender_addr,
            "to": m.to, "cc": m.cc, "subject": m.subject, "labels": _labels(m), "unread": not m.seen,
            "body": body, "quoted_history_hidden": bool(quoted),
            "attachments": [{"filename": text.attachment_name(p), "type": text.attachment_type(p),
                             "size": len(p.get_payload(decode=True) or b"")}
                            for p in text.attachments(parsed)]}


def cmd_show(cfg: Config, args):
    mbox = Mailbox(cfg)
    msgs = thread(mbox, args.id)
    if args.last:
        msgs = msgs[-args.last:]
    out = [_message(mbox, m, args.full, args.max) for m in msgs]
    if args.mark_read:
        mbox.set_seen([m for m in msgs if not m.seen], True)
    mbox.close()
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    for i, m in enumerate(out):
        if i:
            print("\n" + "─" * 60)
        print(f"id {m['id']}  {m['date']}" + ("  UNREAD" if m["unread"] else "")
              + (f"  [{', '.join(m['labels'])}]" if m["labels"] else ""))
        print(f"From: {m['from']}\nTo: {m['to']}" + (f"\nCc: {m['cc']}" if m["cc"] else ""))
        print(f"Subject: {m['subject']}")
        for a in m["attachments"]:
            print(f"Attachment: {a['filename']} ({a['type']}, {a['size'] // 1024} kB)")
        print()
        print(m["body"])
        if m["quoted_history_hidden"]:
            print("[quoted history hidden, --full shows it]")


def cmd_attachments(cfg: Config, args):
    mbox = Mailbox(cfg)
    msgs = mbox.by_msgid([args.id]) or thread(mbox, args.id)
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    for m in msgs:
        for p in text.attachments(mbox.fetch(m)):
            dest = out_dir / text.attachment_name(p)
            dest.write_bytes(p.get_payload(decode=True) or b"")
            print(dest)
    mbox.close()


def cmd_sorted(cfg: Config, args):
    rows = ops.sort_history(limit=args.limit)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    import time
    for e in rows:
        print(f"{e['msgid']}  {time.strftime('%m-%d %H:%M', time.localtime(e['ts']))}  "
              f"{e['choice']:<12} {e['confidence']:.2f}  {e['from'][:24]:<24}  {e['subject']}")


# ------------------------------------------------------------ changing

def _save_undo(recs: list[dict]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UNDO_FILE.write_text(json.dumps(recs, ensure_ascii=False))


def cmd_change(cfg: Config, args):
    mbox = Mailbox(cfg)
    msgs = [m for i in args.ids for m in thread(mbox, i)]
    recs, what = [], args.cmd
    if args.cmd == "archive":
        recs.append(ops.archive(mbox, msgs, None))
    elif args.cmd == "move":
        dst = resolve_folder(mbox, args.category)
        if dst.role:
            _die("move files into a category (label); use archive / trash for the rest")
        recs.append(ops.move(mbox, msgs, None, dst.name))
        what = f"moved to {dst.name}"
    elif args.cmd == "label":
        dst = resolve_folder(mbox, args.category)
        mbox.change_labels(msgs, add=[dst.name])
        recs.append({"kind": "labels", "what": f"Labelled {dst.name}",
                     "items": [{"msgid": m.msgid, "added": [dst.name], "removed": []} for m in msgs]})
        what = f"labelled {dst.name}"
    elif args.cmd == "trash":
        recs.append(ops.trash(mbox, msgs, None))
    elif args.cmd in ("mark-read", "mark-unread"):
        mbox.set_seen(msgs, args.cmd == "mark-read")
    elif args.cmd in ("star", "unstar"):
        newest = {}
        for m in msgs:
            newest[m.thrid] = m
        recs.append(ops.star(mbox, list(newest.values()) if args.cmd == "star" else msgs, args.cmd == "star"))
    if recs:
        _save_undo(recs)
    mbox.close()
    print(f"{what}: {len(args.ids)} conversation(s), {len(msgs)} message(s)"
          + ("  (mail undo reverts)" if recs else ""))


def cmd_undo(cfg: Config, args):
    try:
        recs = json.loads(UNDO_FILE.read_text())
    except (OSError, ValueError):
        _die("nothing to undo")
    mbox = Mailbox(cfg)
    for r in reversed(recs):
        print(ops.undo(mbox, r))
    mbox.close()
    UNDO_FILE.unlink(missing_ok=True)


# ------------------------------------------------------------ drafts

def _body_arg(args) -> str:
    if args.body_file == "-":
        return sys.stdin.read()
    if args.body_file:
        return Path(args.body_file).expanduser().read_text()
    return args.body or ""


def _reply_fields(cfg: Config, mbox: Mailbox, ident: int, all_: bool, forward: bool) -> dict:
    """What the client's Reply / Reply all / Forward fills in for the newest message not sent by me."""
    msgs = mbox.by_msgid([ident]) if ident else []
    if not msgs or msgs[0].thrid == ident:
        conv = thread(mbox, ident)
        me = {i.email.lower() for i in cfg.identities} | {cfg.email.lower()}
        msgs = [m for m in conv if m.sender_addr.lower() not in me] or conv
    m = msgs[-1]
    parsed = mbox.fetch(m)
    body = text.body_text(parsed)
    when = m.date.astimezone().strftime("%a %-d %b %Y %H:%M") if m.date else ""
    ident_addr = cfg.identity_for(*m.recipients).email
    subj = m.subject
    if forward:
        if not subj.lower().startswith(("fwd:", "fw:")):
            subj = "Fwd: " + subj
        quote = (f"{text.FORWARD_HEAD}\nFrom: {m.sender} <{m.sender_addr}>\nDate: {when}\n"
                 f"Subject: {m.subject}\nTo: {m.to}\n" + (f"Cc: {m.cc}\n" if m.cc else "") + f"\n{body}")
        return {"ident": ident_addr, "subject": subj, "quote": quote, "attach_parts": text.attachments(parsed)}
    if not subj.lower().startswith("re:"):
        subj = "Re: " + subj
    to = text.header_addr(parsed, "Reply-To") or formataddr((m.sender, m.sender_addr))
    cc = ""
    if all_:
        mine = {i.email.lower() for i in cfg.identities} | {cfg.email.lower()}
        cc = ", ".join(formataddr((n, a)) for n, a in getaddresses([m.to, m.cc])
                       if a and a.lower() not in mine and a.lower() != m.sender_addr.lower())
    refs = " ".join(x for x in (parsed.get("References", ""), m.message_id) if x).strip()
    return {"ident": ident_addr, "to": to, "cc": cc, "subject": subj,
            "quote": f"On {when}, {m.sender} <{m.sender_addr}> wrote:\n{text.quote(body)}",
            "headers": {"In-Reply-To": m.message_id, "References": refs} if m.message_id else {}}


def _draft_json(d: drafts.Draft) -> dict:
    return {"draft": d.id, "from": d.ident, "to": d.to, "cc": d.cc, "subject": d.subject,
            "body": d.body, "attachments": [text.clean_name(a["filename"]) for a in d.attachments],
            "reply": d.reply, "forward": d.forward, "in_gmail": bool(d.gmail_msgid)}


def cmd_draft(cfg: Config, args):
    mbox = Mailbox(cfg)
    if args.reply or args.reply_all or args.forward:
        kw = _reply_fields(cfg, mbox, args.reply or args.reply_all or args.forward,
                           all_=bool(args.reply_all), forward=bool(args.forward))
    else:
        if not args.to:
            _die("a new message needs --to (or use --reply / --reply-all / --forward ID)")
        kw = {"ident": "", "subject": args.subject or ""}
    if args.to:
        kw["to"] = args.to
    if args.cc is not None:
        kw["cc"] = args.cc
    if args.subject:
        kw["subject"] = args.subject
    if args.from_:
        if not any(i.email == args.from_ for i in cfg.identities):
            _die(f"{args.from_} is not one of the identities in config.toml")
        kw["ident"] = args.from_
    kw["ident"] = kw["ident"] or (cfg.identities[0].email if cfg.identities else cfg.email)
    d = drafts.new(body=_body_arg(args), **kw)
    for path in args.attach or []:
        p = Path(path).expanduser()
        drafts.add_attachment(d, p.name, mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                              p.read_bytes())
    d.edited = True
    drafts.save(d)
    drafts.sync_to_gmail(cfg, mbox, d)
    mbox.close()
    out = _draft_json(d)
    print(json.dumps(out, ensure_ascii=False) if args.json else
          f"draft {d.id} saved (also in Gmail Drafts), from {d.ident} to {d.to}\n"
          f"review: mail drafts --show {d.id}   send: mail send {d.id}")


def _find_draft(did: str) -> drafts.Draft:
    for d in drafts.load_all():
        if d.id == did or d.id.startswith(did):
            return d
    _die(f"no draft {did}; see `mail drafts`")


def cmd_drafts(cfg: Config, args):
    if args.show:
        d = _find_draft(args.show)
        m = drafts.build(cfg, d)
        print(f"From: {m['From']}\nTo: {m['To'] or ''}" + (f"\nCc: {m['Cc']}" if m["Cc"] else "")
              + f"\nSubject: {m['Subject']}")
        for a in d.attachments:
            print(f"Attachment: {text.clean_name(a['filename'])}")
        print()
        print(d.body.rstrip())
        if d.quote:
            print(f"\n[+ {'forwarded message' if d.forward else 'quoted reply'}, "
                  f"{len(d.quote.splitlines())} lines]")
        return
    rows = [_draft_json(d) for d in drafts.load_all()]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return
    for r in rows:
        print(f"{r['draft'][:12]}  {r['from']:<32} → {r['to'][:36]:<36}  {r['subject']}")
    if not rows:
        print("(no drafts)")


def cmd_edit_draft(cfg: Config, args):
    d = _find_draft(args.draft)
    for k in ("to", "cc", "subject"):
        if getattr(args, k) is not None:
            setattr(d, k, getattr(args, k))
    if args.body is not None or args.body_file:
        d.body = _body_arg(args)
    d.edited = True
    drafts.save(d)
    mbox = Mailbox(cfg)
    drafts.sync_to_gmail(cfg, mbox, d)
    mbox.close()
    print(f"draft {d.id} updated")


def cmd_send(cfg: Config, args):
    d = _find_draft(args.draft)
    if not d.to:
        _die("the draft has no recipient")
    ident = cfg.identity_for(d.ident)
    try:
        smtp.send(cfg, ident, drafts.build(cfg, d))
    except smtp.SendError as e:
        _die(f"not sent, kept as a draft: {e}")
    drafts.forget(d)
    mbox = Mailbox(cfg)
    try:
        drafts.purge(mbox)
    except ImapError:
        pass                     # the client finishes the Gmail clean-up on its next start
    mbox.close()
    print(f"sent from {ident.email} to {d.to}")


def cmd_discard(cfg: Config, args):
    d = _find_draft(args.draft)
    drafts.forget(d)
    mbox = Mailbox(cfg)
    drafts.purge(mbox)
    mbox.close()
    print(f"discarded draft {d.id}")


# ------------------------------------------------------------ argparse

def add_parsers(sub):
    def ids(p, help="thread or message id"):
        p.add_argument("ids", nargs="+", type=int, help=help)

    p = sub.add_parser("folders", help="folders and categories with unread counts")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_folders)

    p = sub.add_parser("ls", help="conversations in a folder or category (default Inbox)")
    p.add_argument("folder", nargs="?", default="inbox")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("--unread", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("search", help="Gmail search (from:, subject:, has:attachment, newer_than:7d …)")
    p.add_argument("query")
    p.add_argument("-n", "--limit", type=int, default=25)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="read a conversation")
    p.add_argument("id", type=int)
    p.add_argument("--full", action="store_true", help="keep quoted history in bodies")
    p.add_argument("--last", type=int, default=0, help="only the newest N messages")
    p.add_argument("--max", type=int, default=6000, help="cut each body at N characters (0 = never)")
    p.add_argument("--mark-read", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("attachments", help="save a message's (or thread's) attachments")
    p.add_argument("id", type=int)
    p.add_argument("-o", "--out", default=".")
    p.set_defaults(func=cmd_attachments)

    p = sub.add_parser("sorted", help="what Jev filed recently")
    p.add_argument("-n", "--limit", type=int, default=30)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sorted)

    for name, help in (("archive", "remove from Inbox"), ("trash", "move to Bin"),
                       ("mark-read", "mark read"), ("mark-unread", "mark unread"),
                       ("star", "star"), ("unstar", "unstar")):
        p = sub.add_parser(name, help=f"{help} (whole conversations)")
        ids(p)
        p.set_defaults(func=cmd_change)
    for name, help in (("move", "file into a category: add its label, leave Inbox"),
                       ("label", "add a label, stay where it is")):
        p = sub.add_parser(name, help=help)
        p.add_argument("category")
        ids(p)
        p.set_defaults(func=cmd_change)
    p = sub.add_parser("undo", help="revert the last archive/move/label/trash/star")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("draft", help="write a draft (local + Gmail Drafts); never sends")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--reply", type=int, metavar="ID")
    g.add_argument("--reply-all", type=int, metavar="ID")
    g.add_argument("--forward", type=int, metavar="ID")
    p.add_argument("--to")
    p.add_argument("--cc")
    p.add_argument("--subject")
    p.add_argument("--from", dest="from_", help="identity to send as (default: the one the mail was sent to)")
    p.add_argument("--body")
    p.add_argument("--body-file", help="file with the body, - for stdin")
    p.add_argument("--attach", action="append", metavar="FILE")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("drafts", help="list drafts, or --show one")
    p.add_argument("--show", metavar="DRAFT")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("edit-draft", help="change a draft's fields")
    p.add_argument("draft")
    p.add_argument("--to")
    p.add_argument("--cc")
    p.add_argument("--subject")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.set_defaults(func=cmd_edit_draft)

    p = sub.add_parser("send", help="send a draft")
    p.add_argument("draft")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("discard", help="delete a draft (here and in Gmail)")
    p.add_argument("draft")
    p.set_defaults(func=cmd_discard)
