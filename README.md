# Mail (petrzpav.mail)

A light terminal Gmail client for Omarchy. Arrow keys and Ctrl shortcuts (no vim), categories
that are Gmail labels (so they show up on your phone too), and **Jev** (TypeSafe AI) to file mail
into them, automatically when it's confident or with one key when you ask.

## Setup

```
~/.config/petrzpav-mail/config.toml      account, identities, Jev threshold, [keys] overrides
~/.config/petrzpav-mail/categories.toml  categories + the description Jev reads
~/.config/petrzpav-mail/secrets          GMAIL_APP_PASSWORD=…  TYPESAFE_API_KEY=…   (chmod 600)
```

Dependencies (Textual, httpx, html2text) are installed by `uv` on first run into
`~/.local/share/petrzpav-mail/venv`.

## Use

| | |
|---|---|
| `mail-window` | open the client in its own window, set in iA Writer Mono S (`[window]` in config.toml) |
| `mail` | the client in the current terminal (`F1` lists every shortcut, `Ctrl+K` searches all actions) |
| `mail compose [mailto:…]` | write a new message |
| `mail sort [--dry-run]` | let Jev file new inbox mail (only moves at ≥ threshold confidence) |
| `mail cat ls\|add\|rm\|rename` | manage categories (creates/renames Gmail labels) |
| `mail unread` | inbox unread count |

Replies are sent from the address the message was sent to. Sending goes through Gmail SMTP,
which only accepts From addresses verified as "Send mail as" in Gmail settings. For any other
address, give that identity `smtp = "host:port"` in config.toml and put its password in secrets
as `SMTP_PASSWORD_<ADDRESS_IN_CAPS_WITH_UNDERSCORES>`.

Auto-sort: `systemctl --user enable --now mail-sort.timer` after linking the units from
`systemd/` (`systemctl --user link …`). Every decision is logged to
`~/.local/state/petrzpav-mail/sort.log`. "Jev history" in the palette puts a message back.

## Reading

Mail is grouped into Gmail conversations. Opening one switches to a focus mode: one centered
column, the newest message open, older ones folded to a line (↑↓ move, Enter/Space open), quoted
history folded under "··· earlier messages". Archive, move and delete act on the whole conversation.
