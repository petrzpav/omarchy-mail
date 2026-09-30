"""Run the mail client against a made-up inbox. Jev is real; Gmail is not.

    demo/mail-demo            the client, in this terminal
    demo/mail-demo record     render demo/out/jev-demo.mp4 (headless)

Config comes from demo/config, cache and state go to a fresh temp dir, and the
only thing read from your own setup is TYPESAFE_API_KEY (env or your secrets file).
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def setup():
    """Point the app at a throwaway home, then import it with Gmail swapped out."""
    real_secrets = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "petrzpav-mail/secrets"
    tmp = Path(tempfile.mkdtemp(prefix="mail-demo-"))
    shutil.copytree(HERE / "config", tmp / "config/petrzpav-mail")
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        os.environ[var] = str(tmp / sub)
    sys.path[:0] = [str(HERE.parent / "lib"), str(HERE)]

    import fakegmail
    import inbox
    from mailtui import config, smtp, store

    store.Mailbox = fakegmail.FakeMailbox
    smtp.send = lambda cfg, ident, msg: None          # "sent"

    cfg = config.load()
    from mailtui.config import read_secrets
    key = os.environ.get("TYPESAFE_API_KEY") or read_secrets(real_secrets).get("TYPESAFE_API_KEY", "")
    cfg.secrets = {"TYPESAFE_API_KEY": key}
    if not key:
        sys.exit("demo: TYPESAFE_API_KEY not set (env or ~/.config/petrzpav-mail/secrets)")

    fakegmail.WORLD.labels = [c.name for c in cfg.categories]
    for raw, unread in inbox.messages():
        fakegmail.WORLD.add(raw, labels={"\\Inbox"}, flags=set() if unread else {"\\Seen"})
    return cfg, tmp


def main():
    cfg, tmp = setup()
    try:
        if sys.argv[1:2] == ["record"]:
            import record
            record.main(cfg, sys.argv[2:])
        else:
            from mailtui.app import MailApp
            MailApp(cfg).run()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
