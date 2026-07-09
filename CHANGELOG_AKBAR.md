# CHANGELOG_AKBAR.md

## 2026-07-10 - Russian UI Localization

### Added

- Added simple dictionary-based English/Russian UI localization in `core/i18n.py`.
- Localized main PyQt UI labels, buttons, HUD status text, file upload text, setup overlay, remote overlay, camera labels, footer text, and common UI log messages to Russian by default.
- Added English fallback support through `JARVIS_UI_LANG=en`.

### Changed

- Updated `setup.py` installer messages to use the localization dictionary.
- Added the bilingual UI rule to `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, and `PROJECT_MEMORY.md`.
- Added prompt guidance so assistant-restated UI/system status does not introduce English-only UI labels.

### Rule

From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.

## 2026-07-10 - Reconnect Session Isolation

### Changed

- Added explicit Gemini Live session startup and cleanup helpers in `main.py`.
- Added session generation guards for mic, phone audio, send, receive, playback, briefing, monitor, and proactive tasks.
- Ensured stale mic callbacks from an old session cannot enqueue audio into a new session queue.
- Ensured reconnect starts with fresh queues, reset transient flags, tracked session tasks, and a newly built Live audio config.

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
