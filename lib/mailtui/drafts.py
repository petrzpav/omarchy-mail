"""Drafts: saved locally as you type, mirrored to Gmail's Drafts so they follow you to the phone.

A draft keeps one Message-ID for its whole life; every Gmail save appends the new version and
removes the previous one, the way Gmail's own clients do. A sent or discarded draft is written
to GONE_FILE until Gmail confirms its copies are deleted, so closing the window mid-way leaves
nothing behind: `tidy` finishes the job on the next start.
"""

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import STATE_DIR, Config

DRAFT_DIR = STATE_DIR / "drafts"
GONE_FILE = STATE_DIR / "drafts-gone.json"     # {message_id: gmail_msgid} still to delete from Gmail
_live: dict[str, "Draft"] = {}     # one object per draft, shared by the UI and the sync thread


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
        add_attachment(d, part.get_filename() or "attachment", part.get_content_type(),
                       part.get_payload(decode=True) or b"")
    d.initial = d.snapshot()
    _live[d.id] = d
    return d


def _dir(d: Draft):
    return DRAFT_DIR / d.id


def add_attachment(d: Draft, filename: str, ctype: str, data: bytes):
    folder = _dir(d)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{len(d.attachments)}-{filename.replace('/', '_')}"
    path.write_bytes(data)
    d.attachments.append({"path": str(path), "filename": filename, "ctype": ctype})


def save(d: Draft):
    d.updated = time.time()
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DRAFT_DIR / f"{d.id}.tmp"
    tmp.write_text(json.dumps(asdict(d), ensure_ascii=False))
    tmp.replace(DRAFT_DIR / f"{d.id}.json")


def load_all() -> list[Draft]:
    out = []
    for p in DRAFT_DIR.glob("*.json") if DRAFT_DIR.exists() else []:
        try:
            d = Draft(**json.loads(p.read_text()))
        except (ValueError, TypeError):
            continue
        out.append(_live.setdefault(d.id, d))
    return sorted(out, key=lambda d: d.updated, reverse=True)


def delete(d: Draft):
    _live.pop(d.id, None)
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
    """Put the current version into Gmail's Drafts and drop the previous one."""
    new_id = mbox.append_draft(build(cfg, d).as_bytes())
    d.gmail_msgid = new_id
    if d.id in _live or (DRAFT_DIR / f"{d.id}.json").exists():
        save(d)
    mbox.delete_drafts([v for v in mbox.draft_versions(d.message_id) if v != new_id])
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
