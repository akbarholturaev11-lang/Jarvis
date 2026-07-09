# AGENTS.md

Mandatory startup instructions for AI coding agents working on this repository.

## Required Read Order

Before changing anything, read these files from the project root:

1. `AI_RULES.md`
2. `PROJECT_MEMORY.md`
3. `PROJECT_MAP.md`
4. `NEXT_STEPS.md`

If any of these files are missing, stop and ask Akbar before making code changes.

## Safety Rules

- This is Akbar's personal customized version of `FatihMakes/Mark-XLVIII`.
- Keep changes small, testable, and Mac-compatible.
- Do not edit `config/api_keys.json` unless Akbar explicitly asks.
- Do not touch `.venv/`.
- Do not expose secrets, API keys, private local memory, or payment credentials.
- Always run `git status` before code changes.
- Do not install packages randomly. Explain why first, then use `python -m pip`.
- Preserve original app functionality unless Akbar explicitly asks to change it.
- From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.
- Do not create narrow one-off fixes when a general context layer is needed. Prefer reusable session context, verified action results, and truthful reporting.
- Jarvis must use recent action context before handling vague follow-up commands.
- Vague follow-up commands must go through SessionContext/resolver logic before generic close/settings/send routing.
- For recent YouTube/media/audio/browser playback, stop/pause/o'chir follow-ups must prefer media pause/stop, not browser close or settings close.
- On macOS, pause media first and never close/kill apps for media control without Akbar's confirmation.
- Jarvis must never claim an action succeeded unless success was verified.
- Warning filters must stay narrow and source-specific; do not hide unrelated warnings or change dependency versions for log cleanup.

## Git Commit And Push Rules

- After every reliable change that passes verification/tests, create a clear git commit and push it to GitHub.
- Do not push broken, untested, secret-containing, or uncertain changes.
- Always run `git status` before commit.
- Always verify `.gitignore` protects API keys, local memory, `.venv/`, cache files, and logs before push.
- Use small commits.
- Commit messages must be clear.
- Final reports must include the commit hash and push result.
- If tests fail, do not commit or push unless Akbar explicitly orders it.
- If the change is documentation-only, `py_compile` is enough unless runtime files changed.
- If runtime code changes, run at minimum `.venv/bin/python -m py_compile main.py` and relevant manual/runtime checks.

## Memory Updates

Update `PROJECT_MEMORY.md` only when a change adds durable context that will help a future assistant understand, debug, or safely continue the project.

Do not write small UI edits, typo fixes, temporary experiments, console cleanup, or minor CSS/text changes into `PROJECT_MEMORY.md`.

Implementation changes should be logged in `CHANGELOG_AKBAR.md`.

## Runtime Action Truthfulness

- Keep short-term action context runtime-only unless Akbar explicitly asks for persistence.
- Recent action records should summarize user text and tool parameters; never store API keys, secrets, or long private text fully.
- For vague follow-ups like `o'chir`, `to'xtat`, `yubor`, `yana qil`, `bekor qil`, `shuni yop`, `qayerga yubording?`, and `nima qilding?`, use the last 2-5 meaningful action records before selecting a tool.
- If the last relevant action is media playback, resolve stop/pause/o'chir to media control first.
- If media/browser/message context is low-confidence, ask clarification instead of guessing.
- Verified success may be reported as `Bajarildi.` Failed actions: `Bajara olmadim.` Uncertain actions: `Aniq tasdiqlay olmadim.` Confirmation needed: `Tasdiqlaysizmi?`
- For message sending, do not report `sent` unless the contact/chat and message placement or delivery were verified.

## Reporting

Final reports to Akbar must be in Uzbek.
