# AI_RULES.md

These rules are mandatory for any future AI assistant, Codex, Claude, or code bot working on MARK XLVIII - AkbarCustom.

If a future AI assistant does not read these files first, its work is invalid.

## Required Startup Order

1. Always read `AI_RULES.md` first.
2. Then read `PROJECT_MEMORY.md`.
3. Then read `PROJECT_MAP.md`.
4. Then read `NEXT_STEPS.md`.

## Repository Safety Rules

5. Never expose or commit API keys.
6. Never modify `config/api_keys.json` unless the user explicitly asks.
7. Never modify `.venv/`.
8. Never install packages randomly without explaining why.
9. Prefer `python -m pip` over `pip`.
10. Use Python 3.12.
11. Do not downgrade or upgrade dependencies unless necessary.
12. Before changing code, run `git status`.
13. Before risky changes, create a backup or branch.
14. Keep original functionality working.
15. Changes must be small and testable.
16. After every implementation, update `PROJECT_MEMORY.md` and `CHANGELOG_AKBAR.md`.
17. Final report to the user must be in Uzbek.
18. Do not assume this project is a commercial product. It is Akbar's personal assistant experiment/custom version.
19. Do not talk about "audience" or "market" unless the user explicitly asks.
20. Keep Mac compatibility and personal-use workflow working, but do not hardcode Mac-only behavior when a universal platform layer is needed.

## Git Commit And Push Rules

- After every reliable change that passes verification/tests, create a clear git commit and push it to GitHub.
- Do not push broken, untested, secret-containing, or uncertain changes.
- Always run `git status` before commit.
- Always verify `.gitignore` protects API keys, local memory, `.venv/`, cache files, and logs before push.
- Use small commits.
- Commit message must be clear.
- Final report must include commit hash and push result.
- If tests fail, do not commit/push unless the user explicitly orders it.
- If the change is documentation-only, `py_compile` is enough unless runtime files changed.
- If runtime code changes, run at minimum:
  - `.venv/bin/python -m py_compile main.py`
  - relevant manual/runtime checks

## Memory Discipline

`PROJECT_MEMORY.md` is not a diary and not a changelog dump. It should store only durable project context:

- project purpose
- architecture
- database or memory schema summary
- key files
- payment/subscription/access logic if it is ever added
- important business or personal workflow logic
- important decisions
- known problems
- important changes

Do not store minor UI edits, emoji changes, typo fixes, temporary experiments, console cleanup, or small CSS/text changes in `PROJECT_MEMORY.md`.

Never store secrets in memory files:

- API keys
- bot tokens
- passwords
- real `DATABASE_URL` values
- private links
- payment credentials

Before updating `PROJECT_MEMORY.md`, ask internally:

"Will this help another AI assistant understand, debug, or safely continue this project later?"

If the answer is no, do not update it.

## Runtime Boundaries

- Do not run `main.py` unless the user asks or it is needed for verification.
- Prefer lightweight checks such as `python -m py_compile main.py` for documentation-only work.
- Do not modify runtime logic during documentation/context-foundation tasks.
- Do not overwrite local user memory in `memory/long_term.json`.
- Keep Mac permissions in mind: Microphone, Accessibility, Screen Recording, and Camera may be required.

## Bilingual UI Rule

From now on, every new visible UI text must be added in both English and Russian. Do not add English-only UI labels. Do not add Russian-only UI labels unless user explicitly asks. Keep UI localization simple and maintainable.

UI language must be controlled through `config/settings.json` with `ui_language` set only to `ru` or `en`. Do not introduce arbitrary language codes or require terminal environment variables for normal UI language switching.

## Session Context And Truthful Actions

- Do not create narrow one-off fixes when a general context layer is needed. Prefer reusable session context, verified action results, and truthful reporting.
- Jarvis must use recent action context before handling vague follow-up commands such as `o'chir`, `to'xtat`, `yubor`, `yana qil`, `bekor qil`, `shuni yop`, `oldingi ishni davom ettir`, `qayerga yubording?`, and `nima qilding?`.
- Vague follow-up routing must inspect the last 5 meaningful `SessionContext` actions before generic tool routing. Prefer reusable resolver logic over one-off phrase fixes.
- If recent context is YouTube/media/audio/browser playback, `to'xtat`, `stop`, `pause`, `o'chir`, or `musiqa o'chir` should resolve to media pause/stop before any close/settings action.
- On macOS media stop/pause, send a safe media pause/play-pause command first. Do not close, quit, or kill apps unless Akbar confirms.
- Jarvis must never claim an action succeeded unless success was verified by the tool result.
- Allowed action status language:
  - verified success: `Bajarildi.`
  - failed: `Bajara olmadim.`
  - uncertain: `Aniq tasdiqlay olmadim.`
  - needs confirmation: `Tasdiqlaysizmi?`
- For message sending, never say a message was sent unless the correct target/contact/chat and the message placement or delivery were verified. If verification is not safe, ask for confirmation or report uncertainty.
- If the user corrects Jarvis after an action, attach the correction to recent action context and avoid repeating the same target/tool mistake.
- Warning filters must be narrow and source-specific. Do not hide unrelated warnings/errors, downgrade NumPy, reinstall PyQt6, or patch third-party package code for log cleanup.

## Personal Operations Briefing

- Automatic startup must prepare Akbar's Personal Operations Briefing from verified local sources. It must not fetch generic world news.
- `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, and `personal briefing` must use the existing `personal_briefing` tool path.
- Generic world news may run only for an explicit request such as `dunyo yangiliklari`, `world news`, or `latest news`.
- Personal Briefing local reads must stay on the documented allowlist and read-only Git metadata. Never read `config/api_keys.json` or `memory/long_term.json` for briefing statistics.
- Telegram, Instagram, Messenger, Zerno, or future external statistics must come from a real configured adapter. Missing API/token/config means `not_configured`; never invent counts, revenue, engagement, or other metrics.
- `foyda`, `zarar`, and `next_action` must be evidence-based operational fields. Do not present them as financial profit/loss without a real financial source.
- Keep the briefing intent guard inside the existing Gemini tool/`_execute_tool` route. Do not add a text-only parallel command system that leaves voice commands unprotected.

## Universal Device Intelligence

- Always detect the current platform before platform-specific commands.
- Never assume the device is macOS, Windows, or Linux without checking `DeviceProfile`.
- Always consult `DeviceProfile` before app, browser, media, message, screen, camera, microphone, clipboard, or UI automation actions.
- `DeviceProfile` means what this device can do. `SessionContext` means what happened recently. Tool verification means what actually succeeded.
- Browser routing priority is explicit user browser, recent `SessionContext`, user preferred browser from `DeviceProfile`, system default browser from `DeviceProfile`, installed browser from `DeviceProfile`, then ask.
- Prefer reusable platform adapters in `core/platform_adapters/` over one-off OS fixes.
- Unknown, blocked, unsupported, or permission-dependent capability must be reported as unknown/blocked/unsupported/permission-dependent, not as success.
- `config/device_profile.json` is local operational metadata and must stay gitignored. Commit only `config/device_profile.example.json`.
