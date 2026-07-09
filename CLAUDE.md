# CLAUDE.md

Instructions for Claude, Codex, and other AI coding agents working on MARK XLVIII - AkbarCustom.

## Before Any Work

1. Read `AI_RULES.md`.
2. Read `PROJECT_MEMORY.md`.
3. Read `PROJECT_MAP.md`.
4. Read `NEXT_STEPS.md`.
5. Run `git status`.
6. Summarize the current project state to the user in Uzbek.
7. Ask for confirmation before risky edits.

## During Work

- Keep changes minimal.
- Do not edit API keys.
- Do not touch `.venv/`.
- Do not change dependency versions unless required.
- Do not overwrite the user's local memory.
- Prefer small patches.
- Add tests if possible.
- Keep Mac compatibility.
- Preserve original functionality unless Akbar explicitly asks for a behavior change.
- Use Python 3.12.
- Prefer `python -m pip` over `pip` if package installation is explicitly needed.
- From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.
- Do not create narrow one-off fixes when a general context layer is needed. Prefer reusable session context, verified action results, and truthful reporting.
- Jarvis must use recent action context before handling vague follow-up commands.
- Jarvis must never claim an action succeeded unless success was verified.
- For message sending, never say a message was sent unless the correct target/contact/chat and message placement or delivery were verified.

## After Work

1. Run relevant checks.
2. Update `PROJECT_MEMORY.md` if the change adds durable context.
3. Update `CHANGELOG_AKBAR.md`.
4. Update `NEXT_STEPS.md` if next actions changed.
5. Give an Uzbek report with:
   - what changed
   - files changed
   - tests/checks
   - risks
   - next steps

## Strict Safety Notes

- `config/api_keys.json` is secret local config. Do not print it, commit it, or edit it unless Akbar explicitly asks.
- `memory/long_term.json` is private local assistant memory. Do not print it, commit it, overwrite it, or reset it unless Akbar explicitly asks.
- `.venv/` is local runtime state. Do not modify it.
- `main.py` is high risk because it owns Gemini Live, audio, reconnects, tool declarations, and dispatch.
- `actions/*.py` may depend on matching tool declarations in `main.py`.
- `ui.py` is medium risk because it controls the visible Mac app experience.
