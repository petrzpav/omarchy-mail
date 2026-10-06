# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [Unreleased]

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

[Unreleased]: https://https://github.com/petrzpav/omarchy-mail/compare/staging...dev
[0.2.0]: https://github.com/petrzpav/omarchy-mail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/petrzpav/omarchy-mail/releases/tag/v0.1.0
