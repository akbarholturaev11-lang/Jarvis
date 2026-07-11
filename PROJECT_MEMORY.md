# PROJECT_MEMORY.md

> **Rules live in the `mark-xlviii-workflow` skill**
> (`.claude/skills/mark-xlviii-workflow/`) — the single source of truth for the
> development workflow. This file holds durable **architecture/state/context**, not
> the rules. Read the skill before any change.

## Project Identity

MARK XLVIII - AkbarCustom is Akbar's personal Mac AI assistant experiment and customized version of `FatihMakes/Mark-XLVIII`.

This repository is for personal testing, custom rules, UI changes, AI context files, project memory, and future Akbar-specific features. Do not assume this is a commercial product unless Akbar explicitly says so.

## Origins And Local Paths

- Original repo: `FatihMakes/Mark-XLVIII`
- AkbarCustom GitHub repo: `https://github.com/akbarholturaev11-lang/Jarvis.git`
- Local custom path: `~/Desktop/Mark-XLVIII-AkbarCustom`
- Original test path: `~/Desktop/Mark-XLVIII`

The original test version and AkbarCustom version are separate. AkbarCustom is the active customization workspace.

## Current Mac Install Status

Current known status as of 2026-07-11:

- Project cloned.
- Python 3.12 virtual environment exists.
- Requirements are installed.
- OpenCV is `opencv-python-headless==5.0.0.93` (not `opencv-python`). The GUI OpenCV build bundled its own Qt runtime and hijacked Qt's platform-plugin path, masking PyQt6's Cocoa plugin and crashing `QApplication`. The project uses no OpenCV GUI APIs (only `VideoCapture`/`imencode`/`cvtColor` and capture backends), so headless is the correct build. Do not reinstall plain `opencv-python`.
- PyQt6 6.11.0 / Qt 6.11.1 is installed and verified for the macOS Cocoa GUI path after latest-compatible retesting.
- Qt platform-plugin discovery was once broken (every platform — cocoa/offscreen/minimal — failed with `Could not find the Qt platform plugin "..." in ""` even though the plugin files were present and directly loadable). Fixed by force-reinstalling the same-version Qt wheel: `python -m pip install --force-reinstall --no-deps PyQt6-Qt6==6.11.1`. If this recurs, re-run that reinstall. Never add `QT_PLUGIN_PATH` / `QT_QPA_PLATFORM_PLUGIN_PATH` overrides and never downgrade PyQt6.
- `setup.py` completed.
- `python main.py` runs on Mac.
- Gemini API is connected.
- Microphone/audio works.
- Some Gemini Live reconnect errors can happen, but the app reconnects.
- The `.venv` was rebuilt successfully after a local environment failure.

## What Already Works

- Project cloned.
- Python 3.12 venv created.
- Requirements installed.
- PyQt6 6.11.0 / Qt 6.11.1 installed.
- `setup.py` completed.
- `python main.py` runs.
- Gemini Live connects.
- Microphone starts.
- Audio playback starts.
- Timed reminders keep their OS notification and also speak aloud. A connected app atomically claims the event immediately with a renewable owner lease, waits a bounded time for an idle turn, blocks reminder-originated tool execution, and accepts Gemini Live/Charon delivery only after audio reaches and drains the local playback queue. Unclaimed, timed-out, or incomplete Live delivery falls back to local OS speech, and failed speech delivery is retried a bounded number of times.
- `save_memory` / memory persistence works.
- Mic and phone audio input use a bounded outgoing queue; if it fills, stale queued audio is discarded and the newest chunk is kept so `QueueFull` does not crash or spam logs.
- Gemini Live reconnect handling treats `1006` / keepalive disconnects as recoverable, closes mic/audio state cleanly, prints short terminal status, and retries with 3s, 6s, then 12s backoff.
- Each Gemini Live reconnect creates a new session generation with fresh queues, reset transient flags, tracked session tasks, and fresh Live audio config; old mic/phone/send/receive/play tasks are generation-guarded so stale callbacks cannot write into the new session.
- Automatic startup and the commands `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, and `personal briefing` use the Personal Operations Briefing path instead of generic world news.
- Personal Operations Briefing reads only allowlisted project docs and read-only Git metadata, reports evidence-based `foyda`, `zarar`, and `next_action`, and keeps standalone Telegram/Instagram/Messenger sources `not_configured` until real adapters exist.
- Zerno statistics has a configurable real adapter. It reads the gitignored `config/briefing_sources.json`, takes its token only from `ZERNO_API_TOKEN`, performs a bounded authenticated JSON request, normalizes variable dict/list/nested metric shapes, and reports `connected`, `not_configured`, or `failed` without invented numbers.
- Explicit world news remains on `web_search(mode="news")` for direct requests such as `dunyo yangiliklari`, `world news`, or `latest news`.
- The sounddevice NumPy 2.5 warning filter is centralized and reinstalled immediately before the microphone callback stream while unrelated warnings remain visible.
- Remote mobile control: the phone dashboard (`dashboard/server.py` + `dashboard/static/app.html`) is an installable PWA (`manifest.webmanifest`, `sw.js`, icons) with PIN/QR pairing and device-token reconnect. It auto-reconnects with backoff across Mac sleep/wake instead of dropping to login.
- Control-from-anywhere via Cloudflare Tunnel (`core/remote_tunnel.py`, opt-in through `config/settings.json` → `remote_tunnel`). It wraps `cloudflared`, surfaces the public `trycloudflare.com` URL into QR/PIN pairing, restarts on failure, and reports honest `not_installed` when cloudflared is absent. `/login` is rate-limited for public exposure.
- Keep-awake: while a phone WebSocket client is connected, `main.py` keeps the computer awake through `core/power_manager.py` → adapter `prevent_sleep`/`release_sleep` (macOS `caffeinate`, Windows `SetThreadExecutionState`, Linux `systemd-inhibit`, honest unsupported elsewhere), released after a grace period. Controlled by `keep_awake_enabled` in settings.
- Command automation/macros: `core/capabilities.py` (JARVIS abilities as pickable options) + `core/macros.py` (`config/macros.json`, gitignored). One macro composes several capability phrases into a single command; built from a picker (not free text) in the phone app and the desktop settings window; shared via `/api/capabilities` + `/api/macros`.
- Desktop settings window: a corner ⚙ gear in `ui.py` opens `SettingsOverlay` (remote on/off, Show QR/PIN, keep-awake, RU/EN language, revoke paired devices, connection status, macro builder) with an animated native `ToggleSwitch`. Wired to `main.py` via the single `on_settings_action(action, **kwargs)` callback. The desktop stays native PyQt6; Unlumen UI is animation reference only (web-only per `AI_RULES.md`).

## Known Problems

- Gemini Live can disconnect with `APIError 1006` / keepalive ping timeout; the app should recover automatically with capped backoff.
- DuckDuckGo/news search can rate-limit sometimes.
- Mac permissions may be needed for:
  - Microphone
  - Accessibility
  - Screen Recording
  - Camera
- `config/api_keys.json` is local and must never be committed.
- `config/device_profile.json` is local operational metadata and must never be committed. It may contain private local paths or installed app facts. Commit only `config/device_profile.example.json`.
- `config/briefing_sources.json` and `config/local_env.zsh` are local Zerno setup files and must never be committed. The committed source template is `config/briefing_sources.example.json`.
- `memory/long_term.json` is local personal memory and must never be committed.
- Final Gemini speech truthfulness is still guided by tool metadata rather than mechanically intercepted; action source output must remain explicit and non-fabricated.
- A completed local speech command does not prove the reminder was audible if the output device is muted, unavailable, or the computer cannot play sound at that moment; the notification remains the durable fallback.
- PyQt6 6.11.0 / Qt 6.11.1 was retested on this Mac after removing launcher Qt env overrides and now passes minimal `QApplication`, unit tests, terminal launch, and `Jarvis.command` launcher startup. Keep the launcher free of manual `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM_PLUGIN_PATH`, and `QT_QPA_PLATFORM` overrides unless a future debug run proves they are required.

## Current Purpose

- Test MARK XLVIII on Mac.
- Understand the architecture.
- Add custom rules and context files.
- Later add custom features and UI changes.
- Later take useful ideas into the AuraAI roadmap if needed.

## Current Next Goal

Add the real Zerno URL/token through `scripts/setup_zerno_stats.sh`, verify the connection with `scripts/check_zerno_stats.py`, then manually verify the resulting Personal Operations Briefing in the full Mac/Gemini Live app.

## Architecture Summary

- `main.py` is the high-risk app runtime entry point. It manages Gemini Live, audio input/output, tool declarations, reconnect flow, and action dispatch.
- `ui.py` is the PyQt6 HUD/UI layer.
- `actions/*.py` contains tool implementations for app control, browser control, screen capture, reminders, web search, file processing, code help, proactive behavior, and related tasks.
- `actions/reminder.py` schedules notifications, publishes private one-shot reminder events, uses DeviceProfile for platform selection, provides argv-only macOS/Windows/Linux speech fallback without shell execution, and cleans one-shot macOS LaunchAgents after firing.
- `core/reminder_events.py` validates, atomically claims, retries, and completes the private reminder event files consumed by the process-lifetime Gemini Live bridge in `main.py`.
- `actions/media_control.py` provides safe media pause/play-pause behavior, especially for macOS. It must pause first and must not close/kill apps without confirmation.
- `core/briefing_routing.py` is a narrow intent policy inside the existing command path. It recognizes Personal Operations Briefing phrases, explicit world-news phrases, named external-statistics requests, and defensively corrects a wrong briefing/news tool choice in `main.py::_execute_tool()`.
- `actions/personal_briefing.py` provides the Personal Operations Briefing source registry. `local_projects` reads allowlisted docs and read-only Git metadata; Telegram, Instagram, and Messenger are offline `not_configured` adapters, while Zerno is registered through `actions/zerno_stats.py`.
- `actions/zerno_stats.py` loads only the dedicated ignored Zerno config and environment token, calls the configured JSON endpoint with Bearer authentication, redacts secret-like fields, bounds response/depth/size, and normalizes known plus unknown metrics for the briefing and check script.
- `core/runtime_warnings.py` installs the exact sounddevice/NumPy 2.5 shape warning filter before sounddevice imports and immediately before the microphone stream.
- `core/session_context.py` stores runtime-only short-term action context for the current process. It keeps the last 5 meaningful actions, summarizes sensitive parameters, tracks recent browser/app/message/file/media targets, records verified/failed/uncertain/confirmation status, resolves vague follow-up intents, and attaches user corrections.
- `core/device_profile.py` stores DeviceProfile schema/defaults, privacy scrubbing, summary/query helpers, permission gates, and routing decisions for browser/app/media/message commands.
- `core/environment_discovery.py` creates or refreshes `config/device_profile.json` on first run and through refresh commands.
- `core/remote_tunnel.py` runs/monitors `cloudflared` for remote-from-anywhere access. `core/power_manager.py` is a cross-platform keep-awake facade over the platform adapters. `core/app_settings.py` is the safe read/modify/write layer for non-secret `config/settings.json` keys (`remote_tunnel`, `keep_awake_enabled`) that preserves unrelated keys. `core/capabilities.py` + `core/macros.py` provide the capability registry and macro store for command automation.
- `dashboard/server.py` also serves PWA assets, `/api/capabilities`, `/api/macros`, a public-URL/QR path for the tunnel, and `/login` rate limiting; it exposes `set_public_url`, `set_client_count_callback`, `revoke_devices`, `get_lan_url`.
- `ui.py` adds `ToggleSwitch`, `MacroBuilderOverlay`, `SettingsOverlay`, and a corner gear; `main.py` handles all settings actions via `_handle_settings_action` and keep-awake/tunnel lifecycle.
- `core/platform_adapters/` contains the reusable platform interface and macOS/Windows/Linux adapters for OS info, app/browser/message detection, default browser, media control, launch method, active window capability, screen/camera/audio/clipboard/UI automation capability, and permissions.
- `memory/memory_manager.py` stores and formats long-term user memory in `memory/long_term.json`.
- `core/prompt.txt` controls assistant behavior, language, and tool routing rules.
- `config/api_keys.json` stores local secret configuration and must not be touched unless Akbar explicitly asks.
- `config/device_profile.json` stores local safe operational metadata and is gitignored.
- `config/device_profile.example.json` is the committed schema/template.

## Personal Operations Briefing Rule

Startup and Personal Briefing phrases must use the registered `personal_briefing` action through the existing Gemini tool and central dispatch architecture. Desktop/dashboard text gets an internal route hint, while voice protection is enforced by prompt rules plus the central `_execute_tool()` route guard.

Generic world news is not part of startup or Personal Briefing. It is available only through an explicit user news request and the existing `web_search(mode="news")` action.

The default source registry is:

- `local_projects`: available when allowlisted docs or Git metadata can be read;
- `telegram`: `not_configured`;
- `instagram`: `not_configured`;
- `messenger`: `not_configured`;
- `zerno`: `not_configured` until a real URL and `ZERNO_API_TOKEN` are supplied, then a bounded real JSON adapter that returns `connected` or an honest `failed` result.

No external source may return guessed numbers. For Zerno, missing config/placeholder URL/missing token means `not_configured`, transport or JSON errors mean `failed`, and only a valid JSON response means `connected`.

Named external statistics requests (`instagram`, `telegram`, `messenger`, `channels`, `bots`, `posts`) fall back to the connected Zerno hub when their own standalone adapter is `not_configured`. `actions/personal_briefing.py::_apply_zerno_fallback()` collects Zerno at most once (reusing an already-requested Zerno report) and `_zerno_backed_source()` surfaces only the Zerno metric groups mapped in `_ZERNO_FALLBACK_GROUPS` for that platform. If Zerno is connected but has no platform-specific metrics, the source is reported `not_available` with a clear message and no invented numbers; unrelated Zerno data (for example a generic `posts` group) never becomes fake Instagram/Telegram statistics. A standalone adapter that is actually configured wins; only a `not_configured` standalone result triggers the Zerno fallback. If Zerno is also `not_configured`, the named source stays `not_configured`.

Zerno setup is intentionally two-input: `bash scripts/setup_zerno_stats.sh` asks for the API URL and token, writes only local gitignored files, and `python scripts/check_zerno_stats.py` reuses the production adapter without printing the token. The endpoint is expected to accept Bearer authentication and return JSON; no real URL or token belongs in committed files or project memory. Text returned by Zerno is untrusted external data for display/summary only and must never trigger tools or override user/system intent.

The startup greeting retains the existing read-only use of long-term memory for the user's saved name/language. That memory is not a briefing statistics source and is never passed into `actions/personal_briefing.py`.

## AI Assistant Rule

Every AI assistant working on this repo must read:

1. `AI_RULES.md`
2. `PROJECT_MEMORY.md`
3. `PROJECT_MAP.md`
4. `NEXT_STEPS.md`

After meaningful implementation changes, update `PROJECT_MEMORY.md` when the change adds durable project context, and update `CHANGELOG_AKBAR.md` with what changed.

Do not use `PROJECT_MEMORY.md` as a diary or changelog dump. Store only context that helps another assistant safely understand, debug, or continue the project.

## UI Localization Rule

Visible fixed UI text is localized through a simple dictionary-based English/Russian system in `core/i18n.py`. Russian is the default UI language.

The active UI language is stored in `config/settings.json` as `ui_language`, with only `ru` and `en` allowed. `core/i18n.py` loads `config/settings.json` first, then falls back to `JARVIS_UI_LANG`, then falls back to `ru`. Jarvis can change the setting through typed/remote commands or the `set_ui_language` tool, but the app must be restarted for existing UI labels to fully apply the new language.

From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.

## Session Context And Truthful Action Rule

Jarvis has a runtime-only `SessionContext` / action history layer in `core/session_context.py`. It does not write to `memory/long_term.json`. It keeps only the last 5 meaningful action records and stores summaries for private or long text.

Vague follow-up commands such as `o'chir`, `to'xtat`, `yubor`, `yana qil`, `bekor qil`, `shuni yop`, `oldingi ishni davom ettir`, `qayerga yubording?`, and `nima qilding?` must be resolved from recent action context before selecting a tool. Recent browser/app/contact/file/media context has priority over random defaults.

`resolve_followup_intent(user_text, session_context)` / `SessionContext.resolve_follow_up(...)` returns a resolved intent, target context, confidence, and reason. For recent YouTube/media/audio/browser playback, stop/pause/o'chir follow-ups resolve to media pause/stop before generic browser close or settings close. If confidence is low, Jarvis should ask which app/browser instead of guessing.

User corrections such as `GPT Atlas'da`, `Chrome'da`, `Safari emas`, `hali ham o'ynayapti`, `noto'g'ri`, and `ishlamadi` should attach to the latest relevant action. Corrections may update target app/context or mark a previous unverified media stop as failed so the next retry does not repeat the same target/tool mistake.

Jarvis must never claim action success unless the tool result is `result_status=success` and `verified=true`. If a result is failed, say `Bajara olmadim.` If uncertain, say `Aniq tasdiqlay olmadim.` If confirmation is required, say `Tasdiqlaysizmi?`

For message sending, Jarvis must not say a message was sent unless the contact/chat and message placement or delivery were verified. Desktop automation that cannot verify the contact/chat should return an uncertain draft/attempt result instead of a sent claim.

For macOS media control, Jarvis should send a safe media pause/play-pause command first. If browser media can be verified, it may return verified success; otherwise it must report uncertainty, for example that music stopping could not be confirmed. Closing or killing media apps requires explicit confirmation.

## DeviceProfile And Environment Discovery Rule

Jarvis has a universal Device Intelligence layer. On first run, `core/environment_discovery.py` creates `config/device_profile.json` through `core/platform_adapters/`. Refresh commands rebuild it safely:

- `refresh device profile`
- `rescan device`
- `scan my computer`
- `qurilmani qayta tekshir`
- `kompyuterni qayta o'rgan`
- `Mac'ni qayta tekshir`
- `Windows'ni qayta tekshir`

`DeviceProfile` records operational metadata only: OS, version, architecture, Python/venv, shell, GUI/session type, available browsers, default/preferred browser, messaging apps, media control method, app launch method, active window method, screen/camera/audio/clipboard/UI automation capabilities, permission checklist, and safe project resource paths.

Command routing must use this order:

1. `SessionContext` for recent user/action target.
2. `DeviceProfile` for what this device supports.
3. Tool result verification for what actually succeeded.

Browser routing must prefer explicit user browser, then recent session browser, user preferred browser, system default browser, installed browser, then ask. App/media/message/screen/camera/mic/UI automation commands must not assume a capability exists; unknown means unknown, not success.

## GitHub And Commit Workflow

- GitHub remote should point to `https://github.com/akbarholturaev11-lang/Jarvis.git`.
- Main branch name: `main`.
- After every reliable change that passes verification/tests, create a small clear commit and push it to GitHub.
- Do not commit or push broken, untested, secret-containing, or uncertain changes.
- Before every push, verify `.gitignore` protects `config/api_keys.json`, `memory/long_term.json`, `.venv/`, cache files, compiled Python files, logs, and `.DS_Store`.
- Documentation-only changes can be verified with `.venv/bin/python -m py_compile main.py` unless runtime files changed.
- Runtime code changes require `.venv/bin/python -m py_compile main.py` plus relevant manual/runtime checks.
