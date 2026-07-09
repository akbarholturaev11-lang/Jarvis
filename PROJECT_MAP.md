# PROJECT_MAP.md

This is the project map and local markdown knowledge graph for MARK XLVIII - AkbarCustom.

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
   - Device intelligence and environment discovery in `core/device_profile.py` and `core/environment_discovery.py`
   - Platform adapters in `core/platform_adapters/`

4. Tool/action layer
   - `actions/*.py`
   - `actions/media_control.py` for safe macOS/system media pause/play-pause

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
   - `config/certs/`

8. Local runtime
   - `.venv/`
   - Playwright browsers
   - Mac permissions:
     - Microphone
     - Accessibility
     - Screen Recording
     - Camera

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
-> core/session_context.py
-> core/device_profile.py
-> core/environment_discovery.py
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
- Device profile schema/routing: `core/device_profile.py`
- Environment discovery: `core/environment_discovery.py`
- Platform adapters: `core/platform_adapters/`
- Media control action: `actions/media_control.py`
- UI localization: `core/i18n.py`
- Secret config: `config/api_keys.json`
- Safe local settings: `config/settings.json`
- Local device profile: `config/device_profile.json`
- Safe device profile template: `config/device_profile.example.json`

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
- `SessionContext` records the last 5 meaningful actions, recent browser/app/contact/file/media targets, user corrections, and verified/failed/uncertain/confirmation result status.
- `SessionContext` resolves vague follow-up commands before generic tool routing, including media stop/pause, browser close, message send confirmation, and correction handling.
- `DeviceProfile` records current device capability metadata and is consulted before platform-sensitive app/browser/media/message/permission actions.
- `EnvironmentDiscovery` creates or refreshes `config/device_profile.json` using the current platform adapter.
- Platform adapters detect OS-specific facts through `base.py`, `macos.py`, `windows.py`, and `linux.py`.
- `actions/media_control.py` sends safe media pause/play-pause commands on macOS and only reports verified success when playback state can be confirmed.
- `main.py` reads the Gemini key from `config/api_keys.json`.
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
- `.venv/` is DO NOT TOUCH. It is local runtime state.
- `memory/long_term.json` is PRIVATE. Never commit, expose, overwrite, or reset it unless Akbar explicitly asks.
- `ui.py` is MEDIUM risk. UI changes can affect the Mac app experience.
- `actions/*.py` depends on tool declarations in `main.py`. When changing an action signature, check the matching declaration and dispatch code.
- `actions/media_control.py` must not close, quit, or kill apps by default. It should pause first and report uncertainty when playback cannot be verified.
- `requirements.txt` is HIGH risk. Do not change dependency versions casually.

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
