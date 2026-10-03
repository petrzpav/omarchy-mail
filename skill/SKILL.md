---
name: mail
description: >
  Read, search, sort and answer the user's e-mail (one Gmail account with several identities)
  through the `mail` CLI of the petrzpav.mail Omarchy plugin. Use whenever the user asks about
  their mail or inbox: "what's new in my mail", "find the e-mail from X", "reply to …",
  "draft an answer", "archive / file / trash these", "what did Jev sort", attachments from an
  e-mail, or forwarding something. Prefer it over Gmail web, IMAP scripts or the Google connectors.
---

# Mail (`mail` CLI)

`mail` with no arguments opens the user's TUI client, so never run it bare. Always give it a subcommand.
Every command talks to Gmail over IMAP directly. The client picks up changes on its next refresh.

## Ids

- Each conversation and message has a numeric Gmail id (X-GM-THRID / X-GM-MSGID).
- A thread's id is its first message's id, so any id from `ls`/`search` names the whole conversation.
- `show`, `archive`, `move` etc. accept either kind of id.

## Reading

```
mail folders                       # folders + categories (Gmail labels) with unread counts and descriptions
mail ls [FOLDER] [-n 25] [--unread]  # conversations, newest first; FOLDER = inbox (default), a category, sent, all, spam, trash, starred
mail search 'GMAIL QUERY' [-n 25]  # full Gmail syntax: from:x subject:y has:attachment newer_than:7d label:IG "phrase"
mail show ID [--last N] [--full] [--max 6000] [--mark-read]
mail attachments MSGID -o DIR      # saves files, prints paths
mail sorted [-n 30]                # what Jev (the AI sorter) filed recently and where
```

- `ls` and `search` rows read `thread-id  *★ date  from  subject (count)  [labels]`. A `*` means unread.
- `show` hides quoted history by default; `--full` keeps it.
- For long threads, start with `--last 2` and read more only if needed.
- Every command takes `--json`.
- `show` does not mark the conversation read; `--mark-read` does.

## Changing (whole conversations, like the client)

```
mail archive ID…        mail trash ID…
mail move CATEGORY ID…  # file into a category: add its label, leave the Inbox
mail label CATEGORY ID… # add a label, stay in the Inbox
mail mark-read ID…  mail mark-unread ID…  mail star ID…  mail unstar ID…
mail undo               # reverts the last archive/move/label/trash/star
```

Categories are Gmail labels, listed by `mail cat ls`; their descriptions say what belongs where.
Some categories (e.g. IG) are `keep_in_inbox`: label those with `label`, not `move`.

## Writing: draft first, send only on explicit approval

```
mail draft --reply ID      [--body-file F|-] [--body TEXT] [--attach FILE]…
mail draft --reply-all ID  …
mail draft --forward ID --to ADDR …
mail draft --to ADDR --subject S [--cc …] [--from IDENTITY] …
mail drafts [--show DRAFT]          # list / review (draft ids are long hex; a prefix works)
mail edit-draft DRAFT [--to] [--cc] [--subject] [--body-file F|-]
mail send DRAFT
mail discard DRAFT
```

- `draft` saves the message locally and in Gmail Drafts. The user sees it in the TUI (Ctrl+O) and on the phone.
- `draft` never sends anything.
- Reply/forward fills in the recipients, `Re:`/`Fwd:` subject, threading headers and quoted text, like the client.
- The sending identity is the one the original mail was addressed to.
- `--reply THREAD_ID` answers the newest message in that thread not sent by the user. `--reply MSGID` answers that exact message.
- The body is only what you write above the quote.
- Pass multi-line bodies through stdin with `--body-file -` and a quoted heredoc (`<<'EOF'`).
- **Before `mail send`, show the user the draft (`mail drafts --show ID`) and get an explicit OK. E-mail is outward-facing and can't be unsent.**
- If the user only said "draft"/"prepare", stop at the draft and tell them it's in Drafts.
- Write in the language of the conversation; Czech mail gets Czech replies.
- Match the user's usual tone: look at their earlier replies with `mail search 'in:sent to:ADDR' -n 3` and `mail show` one of them.
- Sign off the way the user does in those sent replies. Don't invent a signature.

## Tips

- Triage: `mail ls --unread`, then `mail ls IG -n 10` (work), then summarize per conversation. Only open (`show --last 1`) what needs a decision.
- "Did X answer?" → `mail search 'from:X newer_than:14d'`.
- Errors land in `~/.local/state/petrzpav-mail/errors.log`.
- Config is in `~/.config/petrzpav-mail/` (config.toml has the identities, categories.toml the categories). Never print the `secrets` file.
