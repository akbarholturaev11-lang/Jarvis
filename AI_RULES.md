# AI_RULES.md

> **⛔ STOP — READ THIS FIRST. This applies to every AI agent (Claude, Codex, or
> any other code bot) working on MARK XLVIII - AkbarCustom. If you skip it, your
> work is invalid.**

The rules that used to live in this file are now maintained in **one canonical
place** so they can't drift out of sync across documents:

## 📌 The single source of truth is the `mark-xlviii-workflow` skill

- **Claude Code** auto-loads it — invoke/apply the **`mark-xlviii-workflow`** skill
  before, during, and after any change.
- **Any other agent** (Codex, etc. — you don't auto-load Claude skills): read these
  two files **in full, by path**, before changing anything:
  1. `.claude/skills/mark-xlviii-workflow/SKILL.md` — the operational checklist
     (Before / During / After workflow).
  2. `.claude/skills/mark-xlviii-workflow/references/detailed-rules.md` — the full
     detailed rules (repository safety, bilingual EN+RU UI, session context &
     truthful-action vocabulary, DeviceProfile, the mandatory cross-platform
     feature contract, Personal Operations Briefing / Zerno semantics, verification,
     memory discipline, git/commit rules, and the high-risk-file list).

Then read the **state/context** docs (these are not rules, but you still need them):
`PROJECT_MEMORY.md`, `PROJECT_MAP.md`, `NEXT_STEPS.md`. Run `git status`, summarize
the state to Akbar **in Uzbek**, and confirm before any risky edit.

## Non-negotiable safety floor (full detail in the skill)

Even before you open the skill, never do these:

- Never expose or commit secrets. Never edit `config/api_keys.json`,
  `memory/long_term.json`, or the local device/Zerno configs unless Akbar
  explicitly asks. Never touch `.venv/`.
- Never claim an action succeeded unless the tool result verified it.
- Every new visible UI string is bilingual **English + Russian**.
- Every new capability is **cross-platform** (macOS/Windows/Linux) or returns an
  explicit honest `unsupported`/`not_available`/`needs_permission`/`not_configured`.
- Keep changes small and testable; verify with `.venv/bin/python -m py_compile
  main.py` (plus `pytest tests/` when a covered module changed); final report to
  Akbar in Uzbek.

If `.claude/skills/mark-xlviii-workflow/` is missing, stop and ask Akbar before
making code changes.
