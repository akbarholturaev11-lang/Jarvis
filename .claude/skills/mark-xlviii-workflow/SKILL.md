---
name: mark-xlviii-workflow
description: >-
  Mandatory development workflow and guardrails for the MARK XLVIII / Jarvis
  (AkbarCustom) personal AI-assistant repo. Use this skill BEFORE and DURING any
  work in this repository — implementing a feature, fixing a bug, editing
  main.py / ui.py / actions/*.py / core/*, adding UI text, wiring a Gemini tool,
  running verification, or preparing a commit. It enforces the required startup
  read order, cross-platform (macOS/Windows/Linux) feature parity through
  core/platform_adapters, bilingual EN+RU UI text, truthful verified action
  reporting, secret-file protection, py_compile/pytest verification, and the
  Uzbek after-work report. Trigger it whenever the working directory is
  Mark-XLVIII-AkbarCustom or the task mentions Jarvis, MARK XLVIII, Akbar's
  assistant, personal briefing, Zerno, DeviceProfile, SessionContext, reminder
  events, or platform adapters — even if the user never names the skill. This
  workflow is MANDATORY and UNIVERSAL: every AI agent (Claude, Codex, or any code
  bot) must apply it for any change here. It is the single source of truth for the
  repo's rules, so consult it instead of relying on memory.
---

# MARK XLVIII — AkbarCustom Development Workflow

This repo is Akbar's **personal** Mac AI assistant (a Jarvis-style app): Python
3.12, PyQt6 HUD (`ui.py`), Gemini Live voice + tool-calling (`main.py`), tool
implementations in `actions/*.py`, and runtime intelligence in `core/*`. It is a
custom fork of `FatihMakes/Mark-XLVIII` — **not** a commercial product. Treat it
as personal software: small, reversible, well-explained changes.

**This skill is the single canonical rulebook for the repo.** `SKILL.md` (this
file) is the operational checklist; `references/detailed-rules.md` holds the full
exhaustive detail. `AI_RULES.md`, `CLAUDE.md`, and `AGENTS.md` are now thin
mandatory pointers back to this skill — every agent (Claude auto-loads the skill;
Codex or any other bot must read these two files by path) applies it before,
during, and after any change. If this skill is missing, stop and ask Akbar.

The remaining root docs are **state/context, not rules**, and you still read them:

- `PROJECT_MEMORY.md` — durable architecture, as-built system, known problems.
- `PROJECT_MAP.md` — layer map, dependency graph, "do not edit blindly" list.
- `NEXT_STEPS.md` — the immediate planned work.

For the exhaustive rules (full cross-platform contract, Personal Briefing / Zerno
semantics, git/memory discipline, high-risk-file list), read
`references/detailed-rules.md`.

---

## Phase 1 — Before any work (always)

Do this every session, before touching code. It is cheap and prevents acting on
stale assumptions.

1. You are already reading the rules (this skill). Skim
   `references/detailed-rules.md` if the task touches a detailed domain, then read
   `PROJECT_MEMORY.md`, `PROJECT_MAP.md`, and `NEXT_STEPS.md` for state, map, and
   next work.
2. Run `git status` to see the real working-tree state.
3. **Summarize the current project state to Akbar in Uzbek** — what the project
   is, what's changed in the tree, and what the next step is.
4. **Ask for confirmation before any risky edit** (see the risk map below).

Why: another agent may have left the tree mid-change, and the memory files are
the only durable record of decisions that the code alone doesn't reveal.

---

## Phase 2 — During work (the invariants)

These are the rules that are easy to violate and expensive to get wrong. Each one
has a reason — honor the reason, not just the letter.

### Keep changes small and reversible
Prefer the smallest patch that solves the task. Preserve existing behavior unless
Akbar explicitly asks for a behavior change. Don't refactor opportunistically —
this app has fragile runtime paths (Gemini Live, audio, reconnects).

### Never touch the protected files
These are secret or local-only. Do not print, commit, edit, overwrite, or reset
them unless Akbar **explicitly** asks:

- `config/api_keys.json` — secret keys.
- `memory/long_term.json` — private assistant memory.
- `config/device_profile.json`, `config/briefing_sources.json`,
  `config/local_env.zsh` — local operational/Zerno config (gitignored).
- `.venv/` — local runtime state; never modify.

Commit only the `*.example.json` templates, never the real local files. Before any
push, confirm `.gitignore` still protects all of the above plus caches, logs, and
`.DS_Store`.

### Cross-platform feature parity is mandatory
Every new user-facing capability must work on **macOS, Windows, and Linux** — or
return an explicit honest status. Never ship a silent macOS-only implementation.
The required shape:

1. Define one platform-neutral capability contract.
2. Implement/extend adapters in `core/platform_adapters/` (`base.py` + `macos.py`,
   `windows.py`, `linux.py`).
3. Route execution through `DeviceProfile` / `core/environment_discovery.py`.
4. Use the native implementation for the detected platform.
5. If a platform can't support it, return `unsupported` / `not_available` /
   `needs_permission` / `not_configured` with a clear bilingual explanation —
   never a fake success and never a silent macOS fallback on Windows/Linux.
6. Keep shared logic platform-neutral; isolate OS-specific code in adapters.

Always consult `DeviceProfile` before any app / browser / media / message /
screen / camera / microphone / clipboard / UI-automation action. **Unknown
capability stays unknown** — never assume the OS, an installed browser, Telegram,
or media-control ability without `DeviceProfile` evidence.

### Bilingual UI text (EN + RU), always
Every **new visible UI string** must be added in **both English and Russian** —
never English-only, and never Russian-only unless Akbar explicitly asks. UI text
lives in `core/i18n.py` in the `_MESSAGES` dict: add the same dotted key (e.g.
`status.listening`) to **both** the `"en"` and `"ru"` sub-dicts. UI language is
controlled by `config/settings.json` → `ui_language` (only `ru` or `en`; default
`ru`). Don't invent language codes or require env vars for normal switching.

### Truthful, verified action reporting
Jarvis must **never claim success unless the tool result verified it**
(`result_status=success` and `verified=true`). Use this exact status vocabulary:

- verified success → `Bajarildi.`
- failed → `Bajara olmadim.`
- uncertain → `Aniq tasdiqlay olmadim.`
- needs confirmation → `Tasdiqlaysizmi?`

For message sending: never say a message was sent unless the correct
target/contact/chat **and** the message placement or delivery were verified;
otherwise return an uncertain draft/attempt result. For macOS media control:
pause / play-pause **first** — do not close, quit, or kill apps without Akbar's
confirmation.

### Reuse the context layers — no one-off fixes
Prefer the reusable layers over narrow phrase hacks:

- `core/session_context.py` (`SessionContext`) = what happened recently. Resolve
  vague follow-ups (`o'chir`, `to'xtat`, `yubor`, `yana qil`, `bekor qil`,
  `shuni yop`, `nima qilding?`) through it before generic tool routing. If recent
  context is media/YouTube playback, `stop`/`pause`/`o'chir` route to media
  pause/stop before any close/settings action.
- `DeviceProfile` = what this device can do. `SessionContext` = what happened.
  Tool verification = what actually succeeded.

### Preserve the Personal Operations Briefing route
Startup and the phrases `men uydaman`, `uydaman`, `ishga qaytdim`,
`loyihalarimni tekshir`, `statistikani ayt`, `personal briefing` use the
`personal_briefing` tool path — **not** generic world news. Generic world news is
explicit-only (`dunyo yangiliklari`, `world news`, `latest news`) via
`web_search(mode="news")`. Missing Telegram/Instagram/Messenger/Zerno config is
`not_configured` — never invent counts, revenue, or engagement. Briefing reads
stay on the allowlist; never read `config/api_keys.json` or
`memory/long_term.json` as briefing data.

### Environment discipline
Use Python 3.12. Prefer `python -m pip` over `pip`, and only install a package
after explaining why. Don't upgrade/downgrade dependencies unless required —
`requirements.txt` is high-risk (e.g. OpenCV must stay
`opencv-python-headless`; don't reinstall plain `opencv-python` or downgrade
PyQt6). Keep runtime-warning filters narrow and source-specific; don't patch
third-party packages to clean logs.

### High-risk files — confirm before editing
- `main.py` — owns Gemini Live, audio, reconnects, tool declarations, dispatch.
- `requirements.txt` — dependency versions.
- `actions/reminder.py` + `core/reminder_events.py` + the consumer in `main.py`
  — one delivery path; preserve atomic claim, leases, bounded waits, playback
  confirmation, retry, and notification fallback.
- `ui.py` — medium risk; affects the visible Mac app.
- When changing an `actions/*.py` signature, update the matching tool declaration
  and dispatch in `main.py`.

Consult the full "Do Not Edit Blindly" list in `PROJECT_MAP.md` before editing any
of these.

---

## Phase 3 — After work (verify, record, report)

### Verify
Never report done without running checks. Minimum bar:

```bash
.venv/bin/python -m py_compile main.py     # always, for any runtime change
```

When you touched a module covered by `tests/`, run the relevant tests too:

```bash
.venv/bin/python -m pytest tests/ -q                 # full suite
.venv/bin/python -m pytest tests/test_<module>.py -q # focused
```

(Existing suites: `test_briefing_routing`, `test_device_profile`,
`test_main_briefing_dispatch`, `test_main_reminder_events`,
`test_personal_briefing`, `test_reminder`, `test_reminder_events`,
`test_runtime_warnings`, `test_session_context`, `test_zerno_fallback`,
`test_zerno_stats`.) For a cross-platform feature, add tests for platform routing,
each OS's behavior, and the unsupported/fallback path. Documentation-only changes
can be verified with `py_compile` alone. Do **not** run `main.py` unless Akbar
asks or verification requires it.

### Record durable context
- Update `PROJECT_MEMORY.md` **only** when the change adds durable context
  (purpose, architecture, schema, key files, important decisions, known
  problems). Not a diary — skip minor UI/emoji/typo/console tweaks. Never store
  secrets there.
- Update `CHANGELOG_AKBAR.md` for the implementation (format: `## DATE - Title`,
  then `### Problem` / `### Fix` / `### Constraints kept`, or an equivalent
  `### Added` / `### Why` / `### Verification`).
- Update `NEXT_STEPS.md` if the next actions changed.

### Report in Uzbek
Always give the final report to Akbar **in Uzbek**, covering:

- **Nima o'zgardi** — what changed.
- **Qaysi fayllar** — files changed.
- **Tekshiruvlar** — tests/checks run and their result (say plainly if something
  failed or was skipped).
- **Xavflar** — risks.
- **Keyingi qadamlar** — next steps.

### Commit only when asked
Commit/push only when Akbar asks. Don't commit broken, untested, or
secret-containing changes. Use small commits with a clear message, run
`git status` first, and include the commit hash + push result in the report. If
tests fail, don't commit unless Akbar explicitly orders it.

---

## Quick reference — the layer map

| Layer | Where | Note |
| --- | --- | --- |
| Entry / Gemini Live / dispatch | `main.py` | HIGH risk |
| UI / HUD | `ui.py` | medium risk |
| Tools | `actions/*.py` | keep declaration in `main.py` in sync |
| Session memory | `core/session_context.py` | resolve vague follow-ups |
| Device intelligence | `core/device_profile.py`, `core/environment_discovery.py` | consult before OS actions |
| Platform adapters | `core/platform_adapters/{base,macos,windows,linux}.py` | parity lives here |
| Briefing | `actions/personal_briefing.py`, `actions/zerno_stats.py`, `core/briefing_routing.py` | no invented stats |
| Reminders | `actions/reminder.py`, `core/reminder_events.py` | one delivery path |
| Localization | `core/i18n.py` (`_MESSAGES`), `config/settings.json` | EN+RU, `ru`/`en` only |
| Long-term memory | `memory/memory_manager.py`, `memory/long_term.json` | private — don't touch |
| Prompt/rules | `core/prompt.txt`, `AI_RULES.md`, `CLAUDE.md`, `AGENTS.md` | canonical rules |

When in doubt, the four canonical docs win over this summary — read them.
