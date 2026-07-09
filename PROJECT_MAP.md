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

4. Tool/action layer
   - `actions/*.py`

5. Memory layer
   - `memory/`
   - `memory/memory_manager.py`
   - `memory/long_term.json`

6. Prompt/rules layer
   - `core/prompt.txt`
   - `AI_RULES.md`
   - `CLAUDE.md`
   - `PROJECT_MEMORY.md`

7. Config layer
   - `config/api_keys.json`
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
-> actions/web_search.py
-> actions/file_processor.py
-> actions/code_helper.py
-> actions/dev_agent.py
-> actions/proactive.py
-> core/prompt.txt
-> config/api_keys.json
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
- Secret config: `config/api_keys.json`

### Edges

- Akbar owns `Mark-XLVIII-AkbarCustom`.
- `Mark-XLVIII-AkbarCustom` is customized from `FatihMakes/Mark-XLVIII`.
- `Mark-XLVIII-AkbarCustom` pushes to `https://github.com/akbarholturaev11-lang/Jarvis.git`.
- `main.py` uses `ui.py`.
- `main.py` declares Gemini tools.
- Gemini tool calls are dispatched by `main.py`.
- Tool calls execute code in `actions/*.py`.
- `main.py` loads `core/prompt.txt`.
- `main.py` reads the Gemini key from `config/api_keys.json`.
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
- `.venv/` is DO NOT TOUCH. It is local runtime state.
- `memory/long_term.json` is PRIVATE. Never commit, expose, overwrite, or reset it unless Akbar explicitly asks.
- `ui.py` is MEDIUM risk. UI changes can affect the Mac app experience.
- `actions/*.py` depends on tool declarations in `main.py`. When changing an action signature, check the matching declaration and dispatch code.
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
