"""Drafts: saved locally as you type, mirrored to Gmail's Drafts so they follow you to the phone.

A draft keeps one Message-ID for its whole life; every Gmail save appends the new version and
removes the previous one, the way Gmail's own clients do. A sent or discarded draft is written
to GONE_FILE until Gmail confirms its copies are deleted, so closing the window mid-way leaves
nothing behind: `tidy` finishes the job on the next start.

The client and the `mail` command (an AI agent) both write drafts. Every save is a new revision
and only goes through on top of the revision it was made from (`Changed` otherwise), so nobody
writes back a stale copy; a draft sent or discarded meanwhile raises `Gone` instead of coming back.
"""

import fcntl
import json
import shutil
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid

from . import text
from .config import STATE_DIR, Config

DRAFT_DIR = STATE_DIR / "drafts"
GONE_FILE = STATE_DIR / "drafts-gone.json"     # {message_id: gmail_msgid} still to delete from Gmail
LOCK_FILE = STATE_DIR / "drafts.lock"
FIELDS = ("ident", "to", "cc", "subject", "body")   # what compose edits


class Changed(Exception):
    """Saved elsewhere since this copy was read; `draft` is that newer version."""
    def __init__(self, draft: "Draft"):
        super().__init__(draft.id)
        self.draft = draft


class Gone(Exception):
    """Sent or discarded elsewhere since this copy was read."""


@dataclass
class Draft:
    id: str
    ident: str
    to: str = ""
    cc: str = ""
    subject: str = ""
    body: str = ""                                    # what I write
    quote: str = ""                                   # the message replied to or forwarded, sent below the body
    headers: dict = field(default_factory=dict)       # In-Reply-To / References of a reply
    attachments: list = field(default_factory=list)   # [{"path", "filename", "ctype"}]
    message_id: str = ""
    gmail_msgid: int = 0                              # the version currently in Gmail's Drafts
    updated: float = 0.0
    initial_body: str = ""                            # what compose started with (quote of a reply)
    initial: list = field(default_factory=list)       # [to, cc, subject, body] when compose opened
    edited: bool = False                              # ever differed from `initial`
    rev: int = 0                                      # saves so far (0: never saved here)
    synced: int = 0                                   # the revision that is in Gmail's Drafts

    @property
    def title(self) -> str:
        return self.subject or "(no subject)"

    def snapshot(self) -> list:
        return [self.to, self.cc, self.subject, self.body.strip()]

    @property
    def forward(self) -> bool:
        from .text import FORWARD_HEAD
        return self.quote.startswith(FORWARD_HEAD)

    @property
    def reply(self) -> bool:
        """Answers the quoted message (a forward only passes it on)."""
        return bool(self.quote) and not self.forward

    @property
    def untouched(self) -> bool:
        """Opened and closed without changing anything: leave no draft behind."""
        if self.snapshot() != (self.initial or self.snapshot()):
            self.edited = True
        return not self.edited and not self.gmail_msgid


def new(ident: str, to="", cc="", subject="", body="", quote="", headers=None, attach_parts=None,
        message_id="", gmail_msgid=0) -> Draft:
    d = Draft(id=uuid.uuid4().hex, ident=ident, to=to, cc=cc, subject=subject, body=body, quote=quote,
              headers=dict(headers or {}), gmail_msgid=gmail_msgid, initial_body=body,
              message_id=message_id or make_msgid(domain=ident.split("@")[-1]))
    for part in attach_parts or []:
        add_attachment(d, text.attachment_name(part), text.attachment_type(part),
                       part.get_payload(decode=True) or b"")
    _clean(d)
    d.initial = d.snapshot()
    return d


def _dir(d: Draft):
    return DRAFT_DIR / d.id


def add_attachment(d: Draft, filename: str, ctype: str, data: bytes):
    filename = text.clean_name(filename)
    folder = _dir(d)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{len(d.attachments)}-{filename}"
    path.write_bytes(data)
    d.attachments.append({"path": str(path), "filename": filename, "ctype": text.clean_type(ctype)})


def _readable(addrs: str) -> str:
    """Names in To / Cc as people read them, never RFC 2047 encoded-words (=?utf-8?b?…?=),
    and never with a sender's control characters."""
    if "=?" not in addrs:
        return text.clean_line(addrs)
    from .imap import decode
    return ", ".join(text.formataddr((decode(n), a)) for n, a in getaddresses([addrs]) if a)


def _clean(d: Draft) -> Draft:
    """Sender text reaches drafts (a Reply-To name, quoted headers, a draft from Gmail): keep
    every field terminal-safe, also in drafts saved before this was done."""
    d.to, d.cc = _readable(d.to), _readable(d.cc)
    d.subject = text.clean_line(d.subject)
    d.body, d.quote, d.initial_body = text.clean(d.body), text.clean(d.quote), text.clean(d.initial_body)
    d.headers = {text.clean_line(str(k)): text.clean_line(str(v)) for k, v in d.headers.items()}
    for a in d.attachments:
        a["filename"] = text.clean_name(a["filename"])
        a["ctype"] = text.clean_type(a.get("ctype"))
    return d


def fields(d: Draft) -> dict:
    """The fields compose edits, as a save would store them."""
    c = _clean(replace(d, attachments=[dict(a) for a in d.attachments]))
    return {k: getattr(c, k) for k in FIELDS}


@contextmanager
def _locked():
    """One writer at a time: the client's threads, a second window and the `mail` command."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _read(path) -> Draft | None:
    try:
        d = _clean(Draft(**json.loads(path.read_text())))
    except (OSError, ValueError, TypeError):
        return None
    d.rev = max(d.rev, 1)       # saved before revisions were counted: still a saved draft
    return d


def _write(d: Draft):
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DRAFT_DIR / f"{d.id}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(asdict(d), ensure_ascii=False))
    tmp.replace(DRAFT_DIR / f"{d.id}.json")


def load(did: str) -> Draft | None:
    """The saved version of a draft, None when there is none (never saved, sent or discarded)."""
    return _read(DRAFT_DIR / f"{did}.json")


def save(d: Draft):
    """Store `d` as the next revision. Raises Changed when someone saved a newer one since `d`
    was read, and Gone when it was sent or discarded meanwhile; nothing is written then."""
    _clean(d)
    with _locked():
        disk = load(d.id)
        if disk is None and d.rev:
            raise Gone(d.id)
        if disk is not None and disk.rev != d.rev:
            raise Changed(disk)
        d.rev += 1
        d.updated = time.time()
        _write(d)


def load_all() -> list[Draft]:
    out = [d for p in DRAFT_DIR.glob("*.json") if (d := _read(p))] if DRAFT_DIR.exists() else []
    return sorted(out, key=lambda d: d.updated, reverse=True)


def delete(d: Draft):
    (DRAFT_DIR / f"{d.id}.json").unlink(missing_ok=True)
    shutil.rmtree(_dir(d), ignore_errors=True)


def build(cfg: Config, d: Draft) -> EmailMessage:
    ident = cfg.identity_for(d.ident)
    msg = EmailMessage()
    msg["From"] = Address(ident.name, addr_spec=ident.email)
    if d.to:
        msg["To"] = d.to
    if d.cc:
        msg["Cc"] = d.cc
    msg["Subject"] = d.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = d.message_id
    for k, v in d.headers.items():
        if v:
            msg[k] = v
    msg.set_content("\n\n".join(x for x in (d.body.rstrip(), d.quote) if x) + "\n")
    for a in d.attachments:
        try:
            data = open(a["path"], "rb").read()
        except OSError:
            continue
        maintype, _, subtype = (a.get("ctype") or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=a["filename"])
    return msg


def sync_to_gmail(cfg: Config, mbox, d: Draft) -> int:
    """Put the saved revision `d.rev` into Gmail's Drafts and drop the older copies. Does nothing
    when that revision is no longer the saved one (a newer save syncs itself; a sent or discarded
    draft stays deleted), and takes the copy back when that happened during the upload."""
    rev = d.rev
    disk = load(d.id)
    if disk is None or disk.rev != rev:
        return 0
    new_id = mbox.append_draft(build(cfg, disk).as_bytes())
    with _locked():
        now = load(d.id)
        current = now is not None and now.rev == rev
        if current:
            now.gmail_msgid = new_id
            now.synced = rev
            _write(now)
    if not current:
        mbox.delete_drafts([new_id])
        return 0
    if d.rev == rev:
        d.gmail_msgid, d.synced = new_id, rev
    # Only older copies: X-GM-MSGIDs grow, and a newer one is a newer save's upload.
    mbox.delete_drafts([v for v in mbox.draft_versions(d.message_id) if v < new_id])
    return new_id


def _gone() -> dict[str, int]:
    try:
        return json.loads(GONE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _set_gone(gone: dict[str, int]):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GONE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(gone))
    tmp.replace(GONE_FILE)


def forget(d: Draft):
    """Sent or discarded: delete it here now, and remember to delete it from Gmail (`purge`)."""
    with _locked():
        if d.message_id or d.gmail_msgid:
            _set_gone({**_gone(), d.message_id or str(d.gmail_msgid): d.gmail_msgid})
        delete(d)


def purge(mbox) -> int:
    """Delete every copy of the forgotten drafts from Gmail; returns how many were removed."""
    gone, n = _gone(), 0
    for message_id, msgid in gone.items():
        ids = (mbox.draft_versions(message_id) if message_id.startswith("<") else []) or ([msgid] if msgid else [])
        mbox.delete_drafts(ids)
        n += len(ids)
    if gone:
        with _locked():
            _set_gone({k: v for k, v in _gone().items() if k not in gone})   # keep ones forgotten meanwhile
    return n


def tidy(mbox) -> int:
    """At start: finish interrupted deletions, and drop older Gmail copies of the drafts still
    being written (left when the app closed mid-save). The newest copy always stays."""
    n = purge(mbox)
    for d in load_all():
        versions = mbox.draft_versions(d.message_id)
        keep = {max(versions, default=0), d.gmail_msgid}   # X-GM-MSGIDs only grow
        old = [v for v in versions if v not in keep]
        mbox.delete_drafts(old)
        n += len(old)
    return n


def from_gmail(cfg: Config, m, parsed) -> Draft:
    """Continue a draft that lives in Gmail (written here earlier, or on the phone)."""
    from . import text
    to = str(parsed.get("To") or "")
    ident = cfg.identity_for(*[a for a in (m.sender_addr,) if a]).email
    body, quote = text.body_text(parsed), ""
    if parsed.get("In-Reply-To"):
        body, quote = text.split_quoted(body)
    elif (at := body.find(text.FORWARD_HEAD)) >= 0:
        body, quote = body[:at].rstrip(), body[at:].strip()
    d = new(ident, to=to, cc=str(parsed.get("Cc") or ""), subject=str(parsed.get("Subject") or ""),
            body=body, quote=quote, headers={k: str(parsed.get(k)) for k in ("In-Reply-To", "References") if parsed.get(k)},
            attach_parts=text.attachments(parsed), message_id=str(parsed.get("Message-ID") or ""),
            gmail_msgid=m.msgid)
    d.initial_body, d.edited = "", True
    return d


def adopt(cfg: Config, local: Draft, m, parsed) -> Draft:
    """A draft's Gmail copy `m` was changed elsewhere (on the phone): continue the local draft
    with that text. Its attachments stay the local ones."""
    g = from_gmail(cfg, m, parsed)
    shutil.rmtree(_dir(g), ignore_errors=True)
    for k in ("to", "cc", "subject", "body"):
        setattr(local, k, getattr(g, k))
    local.gmail_msgid, local.synced = m.msgid, local.rev + 1    # what save() makes it: Gmail has it
    try:
        save(local)
    except (Changed, Gone):
        pass                    # changed here meanwhile: compose shows the saved version
    return load(local.id) or local
