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
20. Focus on Mac compatibility and personal-use workflow.

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
