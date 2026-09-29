"""Load ~/.config/petrzpav-mail/{config.toml,categories.toml,secrets}."""

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "petrzpav-mail"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "petrzpav-mail"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "petrzpav-mail"

DEFAULT_KEYS = {
    "goto": "ctrl+g",
    "move": "ctrl+l",
    "jev": "ctrl+j",
    "archive": "ctrl+e",
    "trash": "delete",
    "undo": "ctrl+z",
    "toggle_read": "ctrl+u",
    "star": "ctrl+d",
    "search": "ctrl+f",
    "new": "ctrl+n",
    "reply": "ctrl+r",
    "reply_all": "alt+r",
    "forward": "alt+f",
    "select": "space",
    "select_all": "ctrl+a",
    "add_category": "insert",
    "rename_category": "f2",
    "refresh": "f5",
    "help": "f1",
    "palette": "ctrl+k",
    "send": "ctrl+s",
    "drafts": "ctrl+o",
    "discard": "alt+d",
    "sender": "alt+s",
    "review": "alt+j",
}


@dataclass
class Identity:
    email: str
    name: str = ""
    badge: str = ""
    smtp: str = ""  # "host:port" override; empty = send through Gmail
    send_as: str = ""  # reply from this other identity instead (not a verified Gmail send-as)

    @property
    def secret_key(self) -> str:
        return "SMTP_PASSWORD_" + re.sub(r"[^A-Z0-9]", "_", self.email.upper())


@dataclass
class Category:
    name: str
    description: str = ""
    auto: bool = True
    keep_in_inbox: bool = False  # Jev only labels it; the message stays in the Inbox


@dataclass
class Rule:
    """Mail to `to` is judged important/junk by Jev: junk goes to the Bin, important to the Inbox."""
    to: str
    important: str
    junk: str
    bin_threshold: float = 0.9


@dataclass
class Config:
    email: str
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    jev_model: str = "jev-latest"
    jev_threshold: float = 0.85
    sort_interval: int = 5
    review_auto: bool = True   # grade a message with Jev whenever typing pauses
    review: list = field(default_factory=list)   # jev.Criterion list; empty = jev.CRITERIA
    identities: list[Identity] = field(default_factory=list)
    categories: list[Category] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def password(self) -> str:
        return self.secrets.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

    @property
    def jev_key(self) -> str:
        return self.secrets.get("TYPESAFE_API_KEY", "")

    def identity_for(self, *addresses: str) -> Identity:
        """First configured identity found among the given addresses, else the default."""
        wanted = [a.lower() for a in addresses if a]
        for ident in self.identities:
            if ident.email.lower() in wanted:
                if ident.send_as:
                    return next((i for i in self.identities if i.email == ident.send_as), ident)
                return ident
        return self.identities[0] if self.identities else Identity(self.email)

    def category(self, name: str) -> Category | None:
        return next((c for c in self.categories if c.name == name), None)


def read_secrets(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def load() -> Config:
    raw = tomllib.loads((CONFIG_DIR / "config.toml").read_text())
    acc = raw.get("account", {})
    jev = raw.get("jev", {})
    cfg = Config(
        email=acc["email"],
        imap_host=acc.get("imap_host", "imap.gmail.com"),
        smtp_host=acc.get("smtp_host", "smtp.gmail.com"),
        jev_model=jev.get("model", "jev-latest"),
        jev_threshold=float(jev.get("threshold", 0.85)),
        sort_interval=int(jev.get("interval", 5)),
        review_auto=bool(jev.get("review_auto", True)),
        identities=[Identity(**i) for i in raw.get("identity", [])],
        rules=[Rule(**r) for r in raw.get("rule", [])],
        keys={**DEFAULT_KEYS, **raw.get("keys", {})},
        secrets=read_secrets(CONFIG_DIR / "secrets"),
    )
    cfg.categories = load_categories()
    if raw.get("review"):
        from .jev import Criterion
        cfg.review = [Criterion(**c) for c in raw["review"]]
    return cfg


def load_categories() -> list[Category]:
    path = CONFIG_DIR / "categories.toml"
    if not path.exists():
        return []
    return [Category(**c) for c in tomllib.loads(path.read_text()).get("category", [])]


def _q(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_categories(categories: list[Category]) -> None:
    path = CONFIG_DIR / "categories.toml"
    header = []
    if path.exists():
        for line in path.read_text().splitlines():
            if not line.startswith("#"):
                break
            header.append(line)
    parts = ["\n".join(header)] if header else []
    for c in categories:
        block = f"[[category]]\nname = {_q(c.name)}\ndescription = {_q(c.description)}"
        if not c.auto:
            block += "\nauto = false"
        if c.keep_in_inbox:
            block += "\nkeep_in_inbox = true"
        parts.append(block)
    path.write_text("\n\n".join(parts) + "\n")
