import argparse
import json
import sys

from . import config
from .config import CACHE_DIR, Category


def cmd_unread(cfg, args):
    cache = CACHE_DIR / "unread"
    if args.cached and cache.exists():
        print(cache.read_text().strip())
        return
    from .imap import Mailbox
    mbox = Mailbox(cfg)
    n = mbox.unseen(mbox.folder("inbox").raw)
    mbox.close()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(f"{n}\n")
    print(json.dumps({"unread": n}) if args.json else n)


def cmd_sort(cfg, args):
    from . import ops
    from .imap import Mailbox
    mbox = Mailbox(cfg)
    r = ops.apply_rules(cfg, mbox, dry_run=args.dry_run)
    n = ops.sort_inbox(cfg, mbox, dry_run=args.dry_run, limit=args.limit) + r
    if not args.dry_run:
        print(f"filed {n}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "unread").write_text(f"{mbox.unseen(mbox.folder('inbox').raw)}\n")
    mbox.close()


def cmd_cat(cfg, args):
    from .imap import Mailbox
    cats = cfg.categories
    if args.action == "ls":
        for c in cats:
            print(f"{c.name:<16} {'' if c.auto else '[manual] '}{c.description}")
        return
    mbox = Mailbox(cfg)
    if args.action == "add":
        if cfg.category(args.name):
            sys.exit(f"{args.name} already exists")
        mbox.create_label(args.name)
        cats.append(Category(args.name, args.description or ""))
    elif args.action == "rm":
        cats = [c for c in cats if c.name != args.name]
        if args.delete_label:
            mbox.delete_label(args.name)
    elif args.action == "rename":
        mbox.rename_label(args.name, args.description)
        for c in cats:
            if c.name == args.name:
                c.name = args.description
    config.save_categories(cats)
    mbox.close()


def cmd_backfill(cfg, args):
    from . import ops
    from .imap import Mailbox
    stats = ops.backfill(cfg, Mailbox(cfg), days=args.days, dry_run=args.dry_run)
    print(json.dumps(stats, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(prog="mail", description="Light terminal Gmail client with Jev sorting")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("compose", help="open a new message (accepts a mailto: URL)")
    c.add_argument("url", nargs="?", default="")
    u = sub.add_parser("unread", help="print the inbox unread count")
    u.add_argument("--cached", action="store_true", help="use the last known count, no network")
    u.add_argument("--json", action="store_true")
    s = sub.add_parser("sort", help="let Jev file new inbox mail into categories")
    s.add_argument("--dry-run", action="store_true", help="show decisions, move nothing")
    s.add_argument("--limit", type=int, default=50, help="newest inbox messages to look at")
    b = sub.add_parser("backfill", help="label older mail with Jev (adds labels only)")
    b.add_argument("--days", type=int, default=180)
    b.add_argument("--dry-run", action="store_true")
    k = sub.add_parser("cat", help="manage categories (Gmail labels)")
    k.add_argument("action", choices=["ls", "add", "rm", "rename"])
    k.add_argument("name", nargs="?")
    k.add_argument("description", nargs="?", help="description (add) or new name (rename)")
    k.add_argument("--delete-label", action="store_true", help="rm: also delete the Gmail label")
    args = p.parse_args()

    cfg = config.load()
    if args.cmd == "unread":
        cmd_unread(cfg, args)
    elif args.cmd == "sort":
        cmd_sort(cfg, args)
    elif args.cmd == "backfill":
        cmd_backfill(cfg, args)
    elif args.cmd == "cat":
        if args.action != "ls" and not args.name:
            p.error("category name required")
        cmd_cat(cfg, args)
    else:
        from .app import MailApp
        MailApp(cfg, compose=args.url if args.cmd == "compose" and args.url else None).run()


if __name__ == "__main__":
    main()
