# PROJECT_MEMORY.md

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

Current known status as of 2026-07-10:

- Project cloned.
- Python 3.12 virtual environment exists.
- Requirements are installed.
- PyQt6 6.11 is installed.
- `setup.py` completed.
- `python main.py` runs on Mac.
- Gemini API is connected.
- Microphone/audio works.
- Some Gemini Live reconnect errors can happen, but the app reconnects.

## What Already Works

- Project cloned.
- Python 3.12 venv created.
- Requirements installed.
- PyQt6 6.11 installed.
- `setup.py` completed.
- `python main.py` runs.
- Gemini Live connects.
- Microphone starts.
- Audio playback starts.
- `save_memory` / memory persistence works.
- Mic and phone audio input use a bounded outgoing queue; if it fills, stale queued audio is discarded and the newest chunk is kept so `QueueFull` does not crash or spam logs.
- Gemini Live reconnect handling treats `1006` / keepalive disconnects as recoverable, closes mic/audio state cleanly, prints short terminal status, and retries with 3s, 6s, then 12s backoff.
- Each Gemini Live reconnect creates a new session generation with fresh queues, reset transient flags, tracked session tasks, and fresh Live audio config; old mic/phone/send/receive/play tasks are generation-guarded so stale callbacks cannot write into the new session.

## Known Problems

- Gemini Live can disconnect with `APIError 1006` / keepalive ping timeout; the app should recover automatically with capped backoff.
- DuckDuckGo/news search can rate-limit sometimes.
- Mac permissions may be needed for:
  - Microphone
  - Accessibility
  - Screen Recording
  - Camera
- `config/api_keys.json` is local and must never be committed.
- `memory/long_term.json` is local personal memory and must never be committed.

## Current Purpose

- Test MARK XLVIII on Mac.
- Understand the architecture.
- Add custom rules and context files.
- Later add custom features and UI changes.
- Later take useful ideas into the AuraAI roadmap if needed.

## Current Next Goal

Build a clear project context foundation so future AI assistants, Codex, Claude, or any code bot can understand the project quickly and work safely.

The initial context foundation is markdown-based instead of an external Graphiti/Gravity dependency because installing a new knowledge graph package is unnecessary risk at this stage.

## Architecture Summary

- `main.py` is the high-risk app runtime entry point. It manages Gemini Live, audio input/output, tool declarations, reconnect flow, and action dispatch.
- `ui.py` is the PyQt6 HUD/UI layer.
- `actions/*.py` contains tool implementations for app control, browser control, screen capture, reminders, web search, file processing, code help, proactive behavior, and related tasks.
- `memory/memory_manager.py` stores and formats long-term user memory in `memory/long_term.json`.
- `core/prompt.txt` controls assistant behavior, language, and tool routing rules.
- `config/api_keys.json` stores local secret configuration and must not be touched unless Akbar explicitly asks.

## AI Assistant Rule

Every AI assistant working on this repo must read:

1. `AI_RULES.md`
2. `PROJECT_MEMORY.md`
3. `PROJECT_MAP.md`
4. `NEXT_STEPS.md`

After meaningful implementation changes, update `PROJECT_MEMORY.md` when the change adds durable project context, and update `CHANGELOG_AKBAR.md` with what changed.

Do not use `PROJECT_MEMORY.md` as a diary or changelog dump. Store only context that helps another assistant safely understand, debug, or continue the project.

## GitHub And Commit Workflow

- GitHub remote should point to `https://github.com/akbarholturaev11-lang/Jarvis.git`.
- Main branch name: `main`.
- After every reliable change that passes verification/tests, create a small clear commit and push it to GitHub.
- Do not commit or push broken, untested, secret-containing, or uncertain changes.
- Before every push, verify `.gitignore` protects `config/api_keys.json`, `memory/long_term.json`, `.venv/`, cache files, compiled Python files, logs, and `.DS_Store`.
- Documentation-only changes can be verified with `.venv/bin/python -m py_compile main.py` unless runtime files changed.
- Runtime code changes require `.venv/bin/python -m py_compile main.py` plus relevant manual/runtime checks.
