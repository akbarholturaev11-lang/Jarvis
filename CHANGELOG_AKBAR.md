# CHANGELOG_AKBAR.md

## 2026-07-10 - Gemini 1006 Reconnect Stability

### Changed

- Treated Gemini Live `1006` / keepalive disconnects as recoverable reconnect events instead of crash-style runtime errors.
- Replaced long reconnect tracebacks with short terminal status lines.
- Added capped reconnect backoff: 3s, 6s, then 12s.
- Cleaned mic/audio queues and session audio state between reconnect attempts.
- Made audio output stream stop/close cleanup tolerant of shutdown-time errors.

## 2026-07-10 - Audio Queue Overflow Guard

### Changed

- Added a guarded outgoing audio queue helper in `main.py`.
- When mic or phone audio fills the outgoing queue, stale queued audio is drained and the newest chunk is kept.
- Prevented `asyncio.QueueFull` from escaping through the mic callback and spamming logs or crashing the runtime.

## 2026-07-10 - GitHub Remote And Commit Rules

### Changed

- Added GitHub commit/push workflow rules to `AI_RULES.md` and `AGENTS.md`.
- Documented AkbarCustom GitHub remote in project memory and project map.
- Clarified that reliable verified changes should be committed and pushed, while broken, untested, secret-containing, or uncertain changes must not be pushed.

## 2026-07-10 - AkbarCustom Initial Context Foundation

Version: AkbarCustom initial context foundation

### Added

- Created AkbarCustom copy context foundation.
- Added project memory and AI instruction files.
- Added markdown-based project map / knowledge graph.
- Added resource guide for important files and risk levels.
- Added next-step tracker for Mac testing and future customization.
- Added AkbarCustom changelog.

### Current Setup Status

- Dependencies installed.
- PyQt6 installed.
- Setup completed.
- `main.py` runs on Mac.
- Gemini connects.
- Microphone/audio works.

### Rule

Future meaningful implementation changes must be logged here.
