# CHANGELOG_AKBAR.md

## 2026-07-11 - Spoken Reminder Delivery

### Added

- Added a private file-based reminder event bridge that lets an active, idle Gemini Live session speak scheduled reminders with the configured Charon voice.
- Added atomic event claiming so Gemini speech and the scheduler fallback do not both speak the same reminder.
- Added local speech fallback through macOS `say`, Windows System.Speech, or Linux `spd-say`/`espeak`, while retaining the existing OS notification.
- Added mechanical tool blocking for reminder-originated Gemini turns, local playback completion checks, renewable per-process claim leases, atomic stale-claim recovery, bounded idle waits, and bounded retries when both Live and system speech fail.

### Changed

- Reminder platform selection now uses DeviceProfile instead of reading OS data from the secret API configuration.
- Reminder scripts use unique IDs, private event files, bounded reminder text, absolute macOS command paths, argv-only subprocess calls without shell execution, quoted Linux `at` paths, and one-shot macOS LaunchAgent cleanup.
- Client-content turns are serialized around reminder delivery; pending microphone chunks are drained/gated before the reminder prompt, and fallback starts only after incomplete Live audio is stopped.

### Verified

- Added focused tests for event validation/claiming/retry, prompt-data isolation, runtime tool blocking, DeviceProfile scheduler routing, safe generated scripts, notification-plus-speech behavior, queued Live dispatch, playback failure, and system fallback.

## 2026-07-11 - Zerno Statistics Integration

### Added

- Added a configurable Zerno source adapter inside Personal Operations Briefing with bounded stdlib HTTP, Bearer authentication, flexible dict/list/nested JSON normalization, secret-field redaction, and explicit `connected` / `not_configured` / `failed` states.
- Added `config/briefing_sources.example.json`, interactive `scripts/setup_zerno_stats.sh`, safe `scripts/check_zerno_stats.py`, and `docs/ZERNO_SETUP.md`.
- Added normalized Zerno metric groups, latest updates, evidence-based `foyda` / `zarar`, next action, confidence, last-check time, and up to three priorities without inventing absent fields.

### Changed

- Personal Briefing now treats a valid Zerno JSON response as connected evidence and renders only metric groups actually returned by the API.
- Telegram/Instagram/Messenger statistics requests also inspect Zerno as the configured operations hub, while standalone source adapters remain honestly `not_configured`.
- Added deterministic Personal Briefing routing for `kanallarimni tekshir` and `botlarimni tekshir`; explicit world news remains on the existing news route.

### Security

- Added Git ignore protection for `config/briefing_sources.json` and `config/local_env.zsh`; real tokens remain environment-only and are never committed, logged, or stored in project memory.
- Setup writes local files atomically with restricted permissions, and the check CLI redacts the full token even in debug mode.

### Verified

- Passed the isolated staged-tree full test suite: 84 tests.
- Passed `py_compile` for `main.py`, Zerno/briefing runtime modules, and the check CLI; also passed setup-script syntax and `git diff --check`.
- Exercised setup with a dummy token in a temporary Git repository only; no real Zerno request was made because no local URL/token is configured.

## 2026-07-11 - PyQt6 Latest Compatible Upgrade

### Changed

- Upgraded the tested GUI runtime pin to PyQt6 6.11.0, PyQt6-Qt6 6.11.1, and PyQt6-sip 13.11.1 on the `test/pyqt-latest-compatible` investigation branch.
- Verified the latest PyQt6/Qt pair with a minimal macOS `QApplication`, `main.py` compile, the unit test suite, terminal startup, and `Jarvis.command` launcher startup.
- Updated project memory to clarify that the launcher must not reintroduce manual Qt platform path overrides unless a future `QT_DEBUG_PLUGINS=1` run proves they are required.

## 2026-07-11 - PyQt6 Cocoa Startup Fix

### Changed

- Pinned the GUI runtime to PyQt6 6.8.1 and PyQt6-Qt6 6.8.2 after the minimal macOS `QApplication` test verified this pair loads the Cocoa platform plugin successfully.
- Removed manual `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, and `QT_QPA_PLATFORM` exports from `scripts/launch_jarvis.command`; Qt now uses PyQt6's bundled plugin discovery instead of launcher-forced paths.
- Documented the verified PyQt6/Qt pair in project memory so future dependency updates do not accidentally reinstall the failing PyQt6 6.11 / Qt 6.11 pair.

## 2026-07-11 - AkbarCustom v0.5 Personal Operations Briefing

### Audited

- Added `COMMAND_ARCHITECTURE_AUDIT.md` with the real voice/text input, Gemini function routing, central dispatch, startup/news, prompt, SessionContext, DeviceProfile, truthfulness, and sounddevice warning flows.
- Confirmed normal intent detection is Gemini function calling, with local handlers only for UI language and DeviceProfile; there is no existing `men uydaman` command handler.
- Confirmed generic world news was hardcoded into the once-per-process startup phase in `main.py`.

### Added

- Added `core/briefing_routing.py`, a narrow policy inside the existing command path for Personal Briefing phrases, explicit world news, named external-statistics requests, and defensive wrong-tool correction.
- Added `actions/personal_briefing.py` with an allowlisted `local_projects` adapter and explicit offline Telegram, Instagram, Messenger, and Zerno `not_configured` adapters.
- Added real central-dispatch, startup, source safety, no-fake-statistics, local docs/Git, and warning-filter tests.
- Added `core/runtime_warnings.py` for the exact sounddevice NumPy 2.5 shape deprecation.

### Changed

- Automatic startup now collects Personal Operations Briefing locally and gives Gemini only the verified report for a short spoken summary. It no longer promises or requests world news.
- `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, and `personal briefing` now route to the registered `personal_briefing` tool.
- Explicit `dunyo yangiliklari`, `world news`, and `latest news` remain on `web_search(mode="news")`; implicit generic world-news calls are guarded.
- Personal Briefing returns evidence-based operational `foyda`, `zarar`, and `next_action`. Missing external integrations return `not_configured` and `statistics=None` without network calls or placeholder numbers.
- The exact sounddevice warning filter is installed before all project sounddevice imports and reapplied immediately before the microphone stream; unrelated deprecations remain visible.
- Added English/Russian Personal Briefing content-title and runtime-log localization.

### Safety

- No API keys, private long-term memory, dependency versions, `.venv` files, third-party package code, or unrelated reconnect/audio logic were changed.
- Personal Briefing reads only its allowlisted docs and read-only Git metadata; paths from Git status are counted but not exposed.

## 2026-07-10 - AkbarCustom v0.4 Universal Device Intelligence

### Added

- Added `core/device_profile.py` for DeviceProfile schema defaults, privacy scrubbing, summary/query helpers, routing decisions, and permission gates.
- Added `core/environment_discovery.py` for first-run and refresh-time environment discovery.
- Added `core/platform_adapters/` with base, macOS, Windows, and Linux adapters.
- Added `config/device_profile.example.json` and gitignored local `config/device_profile.json`.
- Added `device_profile` tool plus direct local refresh/query handling for typed and dashboard commands.
- Added `tests/test_device_profile.py` for profile creation, schema validation, platform detection, browser/media/message routing, permission gating, privacy, and `.gitignore` protection.
- Added `UNIVERSAL_COMPATIBILITY_AUDIT.md`.

### Changed

- `main.py` now creates/loads DeviceProfile at startup and adds a concise DeviceProfile context to the model prompt.
- Tool dispatch now consults `SessionContext` first, then `DeviceProfile`, then tool verification for platform-sensitive actions.
- Browser routing no longer assumes Chrome/Safari; it uses explicit browser, recent session browser, preferred/default browser, installed browser, or asks.
- App, media, messaging, screen/camera, and UI automation paths now have DeviceProfile preflight checks.
- `actions/media_control.py` now uses the platform adapter for non-macOS media control instead of a Mac-only design.
- Updated `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, `PROJECT_MEMORY.md`, `PROJECT_MAP.md`, `NEXT_STEPS.md`, and `core/prompt.txt` with DeviceProfile rules.

### Privacy

- DeviceProfile stores operational metadata only and must not store API keys, tokens, passwords, full conversations, screenshots, audio, or private message/contact contents.
- Local `config/device_profile.json` remains ignored by git; only the safe example schema is committed.

## 2026-07-10 - AkbarCustom v0.3.1 Context Routing, Media Control, Log Cleanup

### Added

- Added `resolve_followup_intent(user_text, session_context)` and strengthened `SessionContext.resolve_follow_up(...)` to return resolved intent, target context, confidence, and reason.
- Added `actions/media_control.py` for safe macOS/system media pause/play-pause handling.
- Added dispatch-level rerouting so high-confidence vague media follow-ups execute media pause instead of generic close/settings close.
- Added tests for media follow-up routing, ChatGPT Atlas target preservation, browser close routing, unknown-context clarification, message confirm-send routing, correction updates, still-playing fallback, and unverified media truthfulness.

### Changed

- Vague stop/pause/o'chir follow-ups now inspect the last 5 meaningful action records before selecting a tool.
- Browser/page `yop` follow-ups resolve to browser close only when recent context supports it.
- Message `yubor` follow-ups use recent message context but require confirmation/verification before any sent claim.
- User corrections such as `GPT Atlas'da`, `Chrome'da`, `Safari emas`, `hali ham o'ynayapti`, and `ishlamadi` update recent runtime action context.
- Unverified media stop/pause returns uncertainty instead of claiming stopped.
- Added a narrow startup warning filter for the repeated `sounddevice` NumPy 2.5 shape `DeprecationWarning` without changing dependencies or hiding unrelated warnings.
- Updated `core/prompt.txt`, AI rules, project memory, project map, next steps, and agent docs with the new routing/truthfulness/media-control rules.

### Rule

Jarvis must use SessionContext before generic routing for vague follow-ups, pause media before any app/browser close, and never claim stopped/sent/opened/closed/done unless the action result is successful and verified.

## 2026-07-10 - UI Language Settings Command

### Added

- Added `config/settings.json` with safe `ui_language` storage.
- Updated `core/i18n.py` to load UI language from `config/settings.json`, then `JARVIS_UI_LANG`, then default to Russian.
- Added strict `ru` / `en` validation for UI language changes.
- Added typed/remote command detection for English/Russian UI switching, including mixed Uzbek commands like `inglis qil` and `rus qil`.
- Added a `set_ui_language` tool so spoken Jarvis commands can change the UI language setting.

### Changed

- UI language changes now return a clear restart message in English or Russian.
- Documented the settings-file language rule in `AI_RULES.md` and `PROJECT_MEMORY.md`.

## 2026-07-10 - AkbarCustom v0.3 Session Context And Truthful Actions

### Added

- Added runtime-only `SessionContext` / action history in `core/session_context.py`.
- Session context stores the last 5 meaningful actions with summarized user text, assistant intent, tool name, parameter summary, target app/context, execution method, result status, verification flag, visible claim, and user correction.
- Added helper tests for last-5 action retention, vague browser follow-up resolution, opened-browser app normalization, uncertain action claims, and correction attachment.

### Changed

- Wired `JarvisLive` tool dispatch to record action context and return structured `result_status`, `verified`, `truthful_user_claim`, and recent action context in tool responses.
- Added vague follow-up handling so recent browser/message/file context can fill missing tool parameters before falling back to defaults.
- Replaced generic `Done.` / fabricated send/open fallbacks with uncertain result language when verification is missing.
- Changed message automation to avoid claiming messages were sent when contact/chat or delivery cannot be verified; default behavior now returns an uncertain draft/attempt result.
- Added truthful-action and recent-context rules to `core/prompt.txt`, `AI_RULES.md`, `AGENTS.md`, `CLAUDE.md`, and `PROJECT_MEMORY.md`.

### Rule

Jarvis must not create narrow one-off fixes when a reusable context layer is needed, must inspect recent action context before vague follow-up commands, and must never claim success unless the tool result is verified.

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
