# PROJECT_MAP.md

> **Rules live in the `mark-xlviii-workflow` skill**
> (`.claude/skills/mark-xlviii-workflow/`) — the single source of truth for the
> development workflow. This file is the **map** (layers, dependency graph, do-not-
> edit-blindly list), not the rules. Read the skill before any change.

This is the project map and local markdown knowledge graph for the JARVIS AkbarCustom fork.

No external Graphiti/Gravity dependency is installed for this foundation step. The markdown graph below is the initial safe knowledge map.

## Project Layers

1. Entry point
   - `main.py`

2. UI layer
   - `ui.py`

3. AI/session layer
   - Gemini Live session in `main.py`
   - Tool declarations in `main.py`
   - Audio input/output constants and queues in `main.py`
   - Reconnect/session handling in `main.py`
   - Runtime action history, follow-up intent routing, corrections, and truthful result status in `core/session_context.py`
   - Narrow Personal Briefing / explicit world-news intent policy in `core/briefing_routing.py`
   - Device intelligence and environment discovery in `core/device_profile.py` and `core/environment_discovery.py`
   - Atomic scheduled-reminder event bridge in `core/reminder_events.py`
   - Platform adapters in `core/platform_adapters/` (now also `prevent_sleep`/`release_sleep`)
   - Narrow runtime warning policy in `core/runtime_warnings.py`
   - Cross-platform keep-awake facade in `core/power_manager.py`
   - Cloudflare remote-access tunnel in `core/remote_tunnel.py`
   - Safe settings read/modify/write in `core/app_settings.py`
   - Command-automation capability registry + macro store in `core/capabilities.py` and `core/macros.py`

4. Tool/action layer
   - `actions/*.py`
   - `actions/media_control.py` for safe macOS/system media pause/play-pause
   - `actions/personal_briefing.py` for allowlisted local operations data and external source status
   - `actions/zerno_stats.py` for configured, bounded, normalized Zerno JSON statistics

5. Memory layer
   - `memory/`
   - `memory/memory_manager.py`
   - `memory/long_term.json`

6. Prompt/rules layer
   - `core/prompt.txt`
   - `core/i18n.py`
   - `AI_RULES.md`
   - `CLAUDE.md`
   - `PROJECT_MEMORY.md`

7. Config layer
   - `config/api_keys.json`
   - `config/settings.json`
   - `config/device_profile.json` (local, gitignored)
   - `config/device_profile.example.json`
   - `config/briefing_sources.json` (local Zerno URL, gitignored)
   - `config/briefing_sources.example.json`
   - `config/local_env.zsh` (local Zerno token export, gitignored)
   - `config/certs/`

8. Local runtime
   - `.venv/`
   - Playwright browsers
   - Mac permissions:
     - Microphone
     - Accessibility
     - Screen Recording
     - Camera

9. Product/release layer
   - Desktop facade and exact-version gate: `core/product_runtime.py`
   - Device proof, activation, purchase, signed update and rollback contracts:
     `core/device_identity.py`, `core/product_activation.py`,
     `core/product_purchase.py`, `core/product_updates.py`,
     `core/update_transaction.py`, `core/macos_update.py`,
     `core/update_startup.py`
   - Secure credential migration: `core/credential_service.py`,
     `core/secure_store.py`
   - Commerce/backend/admin UI: `product_backend/`
   - macOS packaging plan: `packaging/macos/`,
     `scripts/build_macos_release.py`, `requirements-build.txt`

## Dependency Graph

```text
main.py
-> ui.py
-> memory/memory_manager.py
-> actions/open_app.py
-> actions/computer_control.py
-> actions/browser_control.py
-> actions/screen_processor.py
-> actions/reminder.py
-> actions/send_message.py
-> actions/media_control.py
-> actions/web_search.py
-> actions/file_processor.py
-> actions/code_helper.py
-> actions/dev_agent.py
-> actions/proactive.py
-> actions/personal_briefing.py
-> actions/zerno_stats.py
-> core/session_context.py
-> core/briefing_routing.py
-> core/device_profile.py
-> core/environment_discovery.py
-> core/reminder_events.py
-> core/runtime_warnings.py
-> core/platform_adapters/base.py
-> core/platform_adapters/macos.py
-> core/platform_adapters/windows.py
-> core/platform_adapters/linux.py
-> core/i18n.py
-> core/prompt.txt
-> config/api_keys.json
-> config/settings.json
-> config/device_profile.json
```

More detailed runtime relationship:

```text
main.py
-> loads config/api_keys.json
-> loads core/prompt.txt
-> creates JarvisUI from ui.py
-> opens Gemini Live session
-> streams microphone audio to Gemini
-> receives audio/text/tool calls from Gemini
-> applies the narrow Personal Briefing/world-news route guard
-> dispatches tool calls to actions/*.py
-> consults core/session_context.py and core/device_profile.py before platform-sensitive tool execution
-> reads/writes memory through memory/memory_manager.py
```

## Project Knowledge Graph

### Nodes

- User: Akbar
- Project: `Mark-XLVIII-AkbarCustom`
- Original: `FatihMakes/Mark-XLVIII`
- GitHub remote: `https://github.com/akbarholturaev11-lang/Jarvis.git`
- Original local test copy: `~/Desktop/Mark-XLVIII`
- Custom local copy: `~/Desktop/Mark-XLVIII-AkbarCustom`
- Runtime: Python 3.12
- AI backend: Gemini Live API
- UI: PyQt6
- Tools: `actions/*.py`
- Memory manager: `memory/memory_manager.py`
- User memory: `memory/long_term.json`
- Rules: `AI_RULES.md`
- Agent instructions: `CLAUDE.md`
- Project memory: `PROJECT_MEMORY.md`
- Resource guide: `AI_RESOURCES.md`
- Prompt: `core/prompt.txt`
- Runtime session context: `core/session_context.py`
- Scheduled reminder event bridge: `core/reminder_events.py`
- Briefing route policy: `core/briefing_routing.py`
- Personal briefing action/source registry: `actions/personal_briefing.py`
- Zerno statistics adapter/parser: `actions/zerno_stats.py`
- Runtime warning policy: `core/runtime_warnings.py`
- Device profile schema/routing: `core/device_profile.py`
- Environment discovery: `core/environment_discovery.py`
- Platform adapters: `core/platform_adapters/`
- Media control action: `actions/media_control.py`
- UI localization: `core/i18n.py`
- Secret config: `config/api_keys.json`
- Safe local settings: `config/settings.json`
- Local device profile: `config/device_profile.json`
- Safe device profile template: `config/device_profile.example.json`
- Local briefing source config: `config/briefing_sources.json`
- Safe briefing source template: `config/briefing_sources.example.json`
- Local Zerno environment helper: `config/local_env.zsh`

### Edges

- Akbar owns `Mark-XLVIII-AkbarCustom`.
- `Mark-XLVIII-AkbarCustom` is customized from `FatihMakes/Mark-XLVIII`.
- `Mark-XLVIII-AkbarCustom` pushes to `https://github.com/akbarholturaev11-lang/Jarvis.git`.
- `main.py` uses `ui.py`.
- `main.py` declares Gemini tools.
- Gemini tool calls are dispatched by `main.py`.
- Tool calls execute code in `actions/*.py`.
- `main.py` loads `core/prompt.txt`.
- `main.py` owns a runtime `SessionContext` instance from `core/session_context.py`.
- Desktop/dashboard text passes through `core/briefing_routing.py` for an internal Personal Briefing/world-news hint before Gemini; voice uses the same prompt plus central dispatch guard after Gemini selects a tool.
- `main.py::_execute_tool()` applies `apply_briefing_route(...)` before SessionContext and DeviceProfile dispatch so Personal Briefing cannot be replaced by generic world news and explicit world news stays on the existing news action.
- Automatic startup calls `actions/personal_briefing.py` directly for verified local/source-registry output, records it in SessionContext, and sends only that report to Gemini for a short spoken summary.
- `actions/personal_briefing.py` reads allowlisted project docs and read-only Git counts, returns evidence-based operational fields, keeps missing standalone social adapters honest, and registers the configured Zerno adapter.
- `actions/zerno_stats.py` reads only `config/briefing_sources.json` and `ZERNO_API_TOKEN`, performs a bounded Bearer-authenticated JSON GET, sanitizes secret-like values, and normalizes connected/failed/not_configured results for both Personal Briefing and the check CLI.
- `actions/reminder.py` writes a private one-shot event when a scheduled reminder fires. A connected process claims it immediately, queues it until the Live turn is idle, and verifies local audio playback completion; unclaimed or incomplete Live delivery uses local OS speech while retaining the notification.
- `core/reminder_events.py` validates event source, bounded message length, and event ID before an atomic owner claim, renews the active lease, atomically recovers only expired claims, preserves failed delivery for bounded retries, and completes only handled events so reminder text is treated as data and duplicate speech is minimized.
- `SessionContext` records the last 5 meaningful actions, recent browser/app/contact/file/media targets, user corrections, and verified/failed/uncertain/confirmation result status.
- `SessionContext` resolves vague follow-up commands before generic tool routing, including media stop/pause, browser close, message send confirmation, and correction handling.
- `DeviceProfile` records current device capability metadata and is consulted before platform-sensitive app/browser/media/message/permission actions.
- `EnvironmentDiscovery` creates or refreshes `config/device_profile.json` using the current platform adapter.
- Platform adapters detect OS-specific facts through `base.py`, `macos.py`, `windows.py`, and `linux.py`.
- `actions/media_control.py` sends safe media pause/play-pause commands on macOS and only reports verified success when playback state can be confirmed.
- `main.py` reads the Gemini key through `core/credential_service.py`; the OS
  secure store is authoritative and `config/api_keys.json` is legacy read-only
  fallback.
- Frozen builds must pass `core/product_runtime.py` exact-version signed local
  entitlement verification before Gemini runtime starts; source runs are not gated.
- `product_backend/` owns the one-plan account/license/device/release/payment/
  entitlement model, private evidence/artifact storage, one-time device proof,
  bilingual admin panel and deployment factory.
- `product_backend/admin_mfa.py`, `admin_credentials.py`, `admin_mfa_api.py` and
  `api_auth.py` own encrypted TOTP/recovery state, persistent salted password
  hashes, MFA enrollment/step-up/password rotation, bounded sessions, CSRF,
  account-global authentication limits, trusted-proxy resolution and optional
  admin CIDR allowlisting.
- `product_backend/admin_web/static/{manifest.webmanifest,sw.js,app.js}` is the
  isolated Mobile Admin PWA shell. It caches no API/admin records and keeps
  tokens out of browser storage; `docs/MOBILE_ADMIN.md` defines the PWA/native
  boundary. `api_queries.py` owns bounded account/license/release admin reads.
- Payment destinations are loaded only from an external owner-only JSON file;
  they are never committed and are returned only after verified device proof.
- `core/i18n.py` reads and writes the UI language setting in `config/settings.json`.
- `config/settings.json` stores safe non-secret settings such as `ui_language` and only supports `ru` / `en`.
- `memory_manager.py` saves user facts to `memory/long_term.json`.
- `memory_manager.py` formats memory context for prompts.
- `setup.py` installs requirements and Playwright browsers.
- `setup.py` can create or depend on `config/api_keys.json`.
- `core/prompt.txt` controls assistant behavior and tool-routing rules.
- `AI_RULES.md` controls future AI coding assistant behavior.
- `CLAUDE.md` gives startup/during/after workflow for Claude/Codex agents.
- `PROJECT_MEMORY.md` stores durable project context.
- `CHANGELOG_AKBAR.md` records implementation history for AkbarCustom.
- `NEXT_STEPS.md` tracks immediate planned work.

## Do Not Edit Blindly

- `main.py` is HIGH RISK. It controls Gemini Live, audio, reconnects, tool declarations, and dispatch.
- `config/api_keys.json` is SECRET. Never print, commit, or edit it unless Akbar explicitly asks.
- `config/device_profile.json` is LOCAL OPERATIONAL METADATA. It is gitignored because it can contain local paths/app facts. Commit only the example schema.
- `config/briefing_sources.json` and `config/local_env.zsh` are LOCAL ZERNO SETUP. They are gitignored and must never be staged; commit only `config/briefing_sources.example.json`.
- `.venv/` is DO NOT TOUCH. It is local runtime state.
- `memory/long_term.json` is PRIVATE. Never commit, expose, overwrite, or reset it unless Akbar explicitly asks.
- `ui.py` is MEDIUM risk. UI changes can affect the Mac app experience.
- `actions/*.py` depends on tool declarations in `main.py`. When changing an action signature, check the matching declaration and dispatch code.
- `actions/reminder.py`, `core/reminder_events.py`, and the process-lifetime consumer in `main.py` form one delivery path. Preserve immediate atomic claiming, renewable owner leases, bounded idle waits, serialized Live turns, mechanical tool blocking, local playback confirmation, bounded retry, argv-only speech commands, notification fallback, DeviceProfile routing, and the rule that command completion does not prove audibility.
- `actions/media_control.py` must not close, quit, or kill apps by default. It should pause first and report uncertainty when playback cannot be verified.
- `actions/personal_briefing.py` must not read `config/api_keys.json`, `memory/long_term.json`, arbitrary files, or invent external statistics. Zerno may use only its dedicated ignored config/environment adapter; all other missing adapters stay `not_configured`.
- `actions/zerno_stats.py` must never accept a token from committed config, print Authorization headers, return unsanitized secret fields, or report `connected` before valid JSON is received.
- `core/briefing_routing.py` is intentionally narrow. Do not grow it into a parallel command system; normal intent detection remains Gemini tool calling plus central dispatch.
- `core/runtime_warnings.py` must remain limited to the exact sounddevice NumPy 2.5 shape deprecation; unrelated warnings must stay visible.
- `requirements.txt` is HIGH risk. Do not change dependency versions casually.
- `requirements-build.txt`, `packaging/`, release signing keys and updater
  contracts are RELEASE HIGH RISK. Build tooling stays separate from the runtime;
  no adapter may claim install success before exact target entitlement,
  private-copy verification, atomic mutation, fresh-nonce health proof and
  rollback are all independently verified. The macOS development adapter must
  never be selected by source defaults or any frozen runtime.
- Real `config/product.json`, payment-instructions files, backend databases,
  signing keys, activation peppers and admin secrets are LOCAL/DEPLOYMENT data;
  never commit or print them.
- `config/macros.json` is LOCAL user data (gitignored) — user command macros; do not commit. `config/settings.json` is committed but non-secret; write it only through `core/app_settings.py` / `core/i18n.py` so unrelated keys are preserved.
- `core/remote_tunnel.py` must never store cloudflared credentials in the repo (they live in `~/.cloudflared`) and must report honest `not_installed`/`failed` rather than a fake public URL.
- `core/power_manager.py` + adapter `prevent_sleep`/`release_sleep` must return honest `unsupported` on OSes/tools that can't keep awake; never fake success.

## Current Safe Foundation

The current project knowledge system is markdown-based:

- `PROJECT_MEMORY.md`
- `AI_RULES.md`
- `AI_RESOURCES.md`
- `CLAUDE.md`
- `PROJECT_MAP.md`
- `CHANGELOG_AKBAR.md`
- `NEXT_STEPS.md`
- `AGENTS.md`

This is enough for future AI assistants to orient quickly without adding external package risk.
