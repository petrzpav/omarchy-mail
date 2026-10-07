# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [Unreleased]

### Added

- Text of an open e-mail can be selected with the mouse and copied with Ctrl+C.
- Ctrl+click opens a link in an open e-mail and in the message quoted under a reply or forward.
- A Trello card or board link opens in the Trello client when it is installed.

## [0.2.6] - 2026-10-07

### Security

- Message lists, conversations and the Jev history cached by earlier versions are cleaned of escape sequences and control characters before they are shown, so the client is safe right after an upgrade, before the first refresh.

## [0.2.5] - 2026-10-07

### Security

- Attachment content types are cleaned of control characters before `mail show` prints them and before a forward or draft sends them on.

## [0.2.4] - 2026-10-06

### Security

- Sender names and addresses (Reply-To, To, Cc), subjects and quoted headers are cleaned of escape sequences and control characters in replies, forwards and drafts, including drafts saved earlier.

### Fixed

- Replying to a message whose Reply-To name hides a line break no longer fails.

## [0.2.3] - 2026-10-06

### Security

- Attachment names from a sender are cleaned of escape sequences, control characters and folders before they are shown, saved or forwarded, in the client and in the mail command.

## [0.2.2] - 2026-10-06

### Changed

- The Claude Code skill is installed only with `install.sh --skill`.

### Security

- Escape sequences and control characters in a message, its headers and attachment names never reach the terminal.
- The Gmail server's certificate is verified before the app password is sent.

## [0.2.1] - 2026-10-06

### Added

- Instructions for AI agents: the repository follows Flow (ig-flow and ig-changelog skills).

## [0.2.0] - 2026-10-06

### Added

- Scriptable commands (`ls`, `search`, `show`, `attachments`, archive / move / label / trash / star with undo, draft → send) and a Claude Code skill.

### Changed

- F1 help in sections, one key per row, scrollable.

### Fixed

- `install.sh` never stops or replaces someone else's mail-sort timer.
- Drafts from the command line keep names readable in To and Cc, and drafts saved with encoded names heal themselves.

## [0.1.0] - 2026-09-30

### Added

- Light terminal Gmail client: conversations, tabs per category, a focus reader, drafts with autosave and Gmail sync, and a bar widget with the unread count.
- Jev (TypeSafe AI) sorts the inbox on a timer and reviews a message while you write it.
- `Tab` walks the links in the reader; conversations with a draft reply are marked.
- Choosing the sender, and To / Cc autocomplete.
- Compact reply and forward, with the original message below; compose fills the terminal.
- `install.sh` for anyone, with `--remove`; examples and a demo on a made-up inbox.

[Unreleased]: https://github.com/petrzpav/omarchy-mail/compare/staging...dev
[0.2.1]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.0...v0.2.1
[0.2.6]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/petrzpav/omarchy-mail/compare/v0.2.1...v0.2.2
[0.2.1]: https://https://github.com/petrzpav/omarchy-mail/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/petrzpav/omarchy-mail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/petrzpav/omarchy-mail/releases/tag/v0.1.0
