# CLAUDE.md

Instructions for Claude, Codex, and other AI coding agents working on
MARK XLVIII - AkbarCustom.

> **⛔ The full workflow and rules live in ONE canonical place: the
> `mark-xlviii-workflow` skill. Apply it before, during, and after any change.**

## How to load the rules

- **Claude Code**: the **`mark-xlviii-workflow`** skill auto-loads for this repo —
  invoke/apply it. It is the single source of truth (`SKILL.md` = the
  Before/During/After checklist; `.claude/skills/mark-xlviii-workflow/references/detailed-rules.md`
  = the exhaustive rules).
- **Any other agent**: read `.claude/skills/mark-xlviii-workflow/SKILL.md` and
  `.claude/skills/mark-xlviii-workflow/references/detailed-rules.md` in full, by
  path, before changing anything.

The skill covers everything: the required startup read order, cross-platform
(macOS/Windows/Linux) feature parity through `core/platform_adapters`, bilingual
EN+RU UI text in `core/i18n.py`, truthful verified action reporting, the Personal
Operations Briefing / Zerno route, secret-file protection,
`py_compile`/`pytest` verification, memory/changelog updates, and the Uzbek
after-work report.

Also read the **state/context** docs (not rules, but needed): `PROJECT_MEMORY.md`,
`PROJECT_MAP.md`, `NEXT_STEPS.md`. Then run `git status`, summarize the state to
Akbar **in Uzbek**. During repository work, the coding AI agent must use Uzbek for
every work-process message to Akbar and ask for confirmation before any risky edit.

For product, license, payment, update, packaging, or release work, also read
`docs/PRODUCT_RELEASE_CONTRACT.md`. Preserve its one-plan, per-semantic-version
paid-entitlement model: a purchased version remains usable indefinitely, future
semantic versions require separate admin-priced entitlements, there is no
subscription or Lifetime Updates plan, and declining an update never remotely
disables the older purchased version. This is a product target, not permission to
sell or distribute; the upstream-license, PyQt6, branding/assets, and signing
gates in that document must be cleared first.

## Strict safety floor (net if the skill fails to load)

- `config/api_keys.json` is secret local config. Do not print, commit, or edit it
  unless Akbar explicitly asks.
- `memory/long_term.json` is private local assistant memory. Do not print, commit,
  overwrite, or reset it unless Akbar explicitly asks.
- The local device/Zerno configs (`config/device_profile.json`,
  `config/briefing_sources.json`, `config/local_env.zsh`) are gitignored — commit
  only their `*.example.*` templates.
- `.venv/` is local runtime state. Do not modify it.
- `main.py` is high risk: it owns Gemini Live, audio, reconnects, tool
  declarations, and dispatch. `actions/*.py` may depend on matching tool
  declarations in `main.py`. `ui.py` is medium risk.
- Never claim an action succeeded unless it was verified. Every new visible UI
  string is bilingual (EN + RU). Every new capability is cross-platform or returns
  an explicit honest unsupported status. Keep changes minimal; use Python 3.12; do
  not change dependency versions unless required.
- Every AI coding agent working on this repository must communicate with Akbar in
  Uzbek throughout the coding session, including plans, progress updates,
  explanations, questions, warnings, and the final report. This rule applies to
  coding-agent communication and does **not** change Jarvis runtime responses. Keep
  only code, commands, paths, technical identifiers, and exact logs/errors in their
  original form, with the surrounding explanation in Uzbek.

If `.claude/skills/mark-xlviii-workflow/` is missing, stop and ask Akbar before
making code changes.
