# AGENTS.md

Mandatory startup instructions for AI coding agents working on this repository.

> **⛔ The complete rules live in ONE canonical place — the `mark-xlviii-workflow`
> skill. This is MANDATORY for every agent before, during, and after any change.**

## Load the rules first

Because most non-Claude agents do not auto-load Claude Code skills, you **must read
the skill files directly, by path, in full**, before changing anything:

1. `.claude/skills/mark-xlviii-workflow/SKILL.md` — the operational Before / During
   / After checklist.
2. `.claude/skills/mark-xlviii-workflow/references/detailed-rules.md` — the full
   detailed rules: repository safety, required read order, bilingual EN+RU UI,
   session context & truthful-action vocabulary, DeviceProfile, the mandatory
   cross-platform feature contract, Personal Operations Briefing / Zerno semantics,
   verification, memory discipline, git/commit rules, and the high-risk-file list.

(Claude Code users: the `mark-xlviii-workflow` skill auto-loads — just apply it.)

Then read the **state/context** docs (not rules, but needed): `PROJECT_MEMORY.md`,
`PROJECT_MAP.md`, `NEXT_STEPS.md`. If the skill directory or any of these is
missing, stop and ask Akbar before making code changes.

## Every session

1. Load the rules (above).
2. Run `git status`.
3. Summarize the current project state to Akbar **in Uzbek**.
4. Ask for confirmation before any risky edit.

## Non-negotiable safety floor (full detail in the skill)

- This is Akbar's **personal** fork of `FatihMakes/Mark-XLVIII` — not a commercial
  product.
- Never expose or commit secrets. Never edit `config/api_keys.json`,
  `memory/long_term.json`, or the gitignored device/Zerno configs unless Akbar
  explicitly asks. Never touch `.venv/`.
- Never claim an action succeeded unless the tool result verified it.
- Every new visible UI string is bilingual **English + Russian**.
- Every new capability is **cross-platform** (macOS/Windows/Linux) or returns an
  explicit honest `unsupported`/`not_available`/`needs_permission`/`not_configured`
  — never a silent macOS-only implementation.
- Keep changes small and testable; use Python 3.12; don't change dependency
  versions unless required. Verify with `.venv/bin/python -m py_compile main.py`
  (plus `pytest tests/` when a covered module changed).
- Log implementation changes in `CHANGELOG_AKBAR.md`; update `PROJECT_MEMORY.md`
  only for durable context. The final report to Akbar is **in Uzbek**.
