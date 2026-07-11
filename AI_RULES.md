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
the state to Akbar **in Uzbek**. During repository work, the coding AI agent must
also use Uzbek for every work-process message to Akbar, and confirm before any
risky edit.

## Non-negotiable safety floor (full detail in the skill)

Even before you open the skill, never do these:

- Never expose or commit secrets. Never edit `config/api_keys.json`,
  `memory/long_term.json`, or the local device/Zerno configs unless Akbar
  explicitly asks. Never touch `.venv/`.
- Never claim an action succeeded unless the tool result verified it.
- Every AI coding agent working on this repository must communicate with Akbar in
  Uzbek throughout the coding session, including plans, progress updates,
  explanations, questions, warnings, and the final report. This rule applies to
  coding-agent communication and does **not** change Jarvis runtime responses. Code,
  commands, paths, technical identifiers, and exact logs/errors may remain in their
  original form, with the surrounding explanation in Uzbek.
- Every new visible UI string is bilingual **English + Russian**.
- Every new capability is **cross-platform** (macOS/Windows/Linux) or returns an
  explicit honest `unsupported`/`not_available`/`needs_permission`/`not_configured`.
- Keep changes small and testable; verify with `.venv/bin/python -m py_compile
  main.py` (plus `pytest tests/` when a covered module changed); final report to
  Akbar in Uzbek.

If `.claude/skills/mark-xlviii-workflow/` is missing, stop and ask Akbar before
making code changes.

## Unlumen UI Usage Rule — Web Only

Unlumen UI is an approved optional design library for Jarvis web products only.

Allowed use cases:
- Jarvis landing page
- pricing and license purchase pages
- account and subscription dashboard
- onboarding web flow
- device and update management dashboard
- web-based Personal Briefing interface

Technical scope:
- React / Next.js
- Tailwind CSS
- browser-based web UI or PWA

Restrictions:
1. Do not use Unlumen UI as the primary UI framework for:
   - PyQt6 desktop application
   - native macOS application
   - native Windows application
   - native Linux application
   - SwiftUI, Kotlin, Flutter, or React Native mobile applications
2. For native/mobile UI, Unlumen UI may only be used as visual and animation reference.
3. Do not force desktop hover, cursor, or magnetic effects into mobile touch interfaces.
4. Prefer accessible, responsive, touch-friendly components.
5. Do not add the dependency until a real React/Next.js web module exists.
6. Free and Pro component license terms must be verified before commercial release.
7. Pro components must not be used without a valid license.
8. Do not resell Unlumen UI components as a standalone UI kit or template product.
9. Keep Jarvis core, licensing backend, updater, and platform adapters independent from this UI library.
10. A web design task must not modify the desktop assistant runtime unless explicitly required.

Current commercial plan:
- Standard license: user keeps the purchased version; no automatic future updates.
- Lifetime Updates license: user receives supported future updates automatically.
- Unlumen UI may be used for the web purchase and license-management interface for these plans.
