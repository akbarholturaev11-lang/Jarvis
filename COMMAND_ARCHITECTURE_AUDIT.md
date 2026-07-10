# Command Architecture Audit

Audit date: 2026-07-11

Scope: discovery of the current command, startup, news, context, device, result-reporting, and audio-warning architecture before Personal Operations Briefing implementation.

## Executive Finding

AkbarCustom does not have a conventional local intent router for normal commands. It uses a hybrid architecture:

- desktop/dashboard text has two narrow local handlers, then goes to Gemini Live;
- microphone and phone audio go directly to Gemini Live without local STT or intent classification;
- Gemini selects a declared function/tool from the system prompt and tool descriptions;
- `main.py::_execute_tool()` is the central enforcement and action-dispatch layer;
- SessionContext can correct a small set of vague follow-up routes after Gemini selects a tool;
- DeviceProfile preflights selected platform-sensitive tools;
- result truthfulness is returned to Gemini as metadata, but the final spoken claim is not mechanically blocked.

There is no current handler for `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, or `personal briefing`. The generic world-news briefing is a once-per-process startup task. If it appears to react to `men uydaman`, the likely cause is startup concurrency/overlap, not an existing phrase mapping.

## Command Flow Diagram

```text
Desktop text
ui.py MainWindow._send()
  -> JarvisLive._on_text_command()
  -> local UI-language / DeviceProfile handlers (only)
  -> SessionContext.observe_user_text()
  -> SessionContext.build_user_turn_context()
  -> Gemini Live session.send_client_content()

Dashboard text
dashboard/server.py HTTP or WebSocket command endpoint
  -> DashboardServer._command_queue
  -> JarvisLive._process_dashboard_commands()
  -> same local handlers / SessionContext / Gemini path as desktop text

Mac microphone
JarvisLive._listen_audio() / sounddevice.InputStream callback
  -> bounded outgoing PCM queue
  -> JarvisLive._send_realtime()
  -> Gemini Live audio input

Phone microphone
DashboardServer phone-audio WebSocket
  -> DashboardServer._phone_audio_queue
  -> JarvisLive._relay_phone_audio()
  -> same outgoing PCM / Gemini Live path

Gemini Live
  -> input/output transcription in JarvisLive._receive_audio()
  -> Gemini function call selected from TOOL_DECLARATIONS
  -> JarvisLive._execute_tool()
       -> SessionContext apply/resolve/reroute
       -> DeviceProfile preflight for supported branches
       -> manual if/elif action dispatch
       -> actions/*.py implementation
       -> infer_result_status() + truthful_claim()
       -> SessionContext.record_action()
       -> FunctionResponse(result/status/verified/rules) to Gemini
  -> Gemini generates final speech/text
  -> final transcript is logged and stored, not hard-validated
```

## 1. User Input Entry Points

### Desktop text

- `ui.py::_build_input_row()` creates the `QLineEdit` and connects Enter/send to `MainWindow._send()` (`ui.py`, approximately lines 2091-2119).
- `ui.py::MainWindow._send()` reads the text and invokes `on_text_command` on a worker thread (approximately lines 2329-2335).
- `main.py::JarvisLive.__init__()` binds that callback to `JarvisLive._on_text_command()`.
- `main.py::JarvisLive._on_text_command()` cleans the text, handles UI language and DeviceProfile requests locally, observes SessionContext, then sends the turn to Gemini (approximately lines 710-729 before this implementation).
- File upload also creates a synthetic text command through the same callback (`ui.py::_on_file_selected()`).

### Desktop microphone

- `main.py::JarvisLive._listen_audio()` opens `sounddevice.InputStream` and its callback enqueues raw PCM.
- `main.py::JarvisLive._send_realtime()` forwards PCM to Gemini Live.
- There is no local Whisper/Vosk routing in the active main path. `core/stt.py` exists but is not used by `JarvisLive` for command intent.

### Remote dashboard text and audio

- `dashboard/server.py` accepts authenticated text through `/api/command` and the dashboard WebSocket, then pushes it to `_command_queue`.
- `main.py::JarvisLive._process_dashboard_commands()` consumes that queue and uses the same local-handler/SessionContext/Gemini route as desktop text.
- Phone PCM enters through the phone-audio WebSocket, `_phone_audio_queue`, and `JarvisLive._relay_phone_audio()`.

## 2. Intent Detection And Command Routing

Normal natural-language intent detection is primarily Gemini function calling. It is driven by:

- `core/prompt.txt` tool-routing rules;
- `main.py::TOOL_DECLARATIONS` descriptions and schemas;
- Gemini Live conversation context.

The only deterministic pre-Gemini text handlers are currently:

- `JarvisLive._handle_ui_language_command()`;
- `JarvisLive._handle_device_profile_local_command()`.

`JarvisLive._execute_tool()` is the central post-selection router. It:

1. applies SessionContext hints;
2. resolves vague follow-ups;
3. may reroute media/browser behavior or block unconfirmed messages;
4. applies DeviceProfile preflight;
5. dispatches through a large manual `if/elif` chain.

There is no dynamic tool registry despite the `# Plugin system` comment. Adding a real tool currently requires synchronized edits to:

1. the action import in `main.py`;
2. `TOOL_DECLARATIONS`;
3. the `_execute_tool()` dispatch chain;
4. `core/prompt.txt`.

Current routes relevant to this audit:

- app opening: Gemini `open_app` -> DeviceProfile preflight -> `actions/open_app.py::open_app()`;
- web/news: Gemini `web_search` with `mode` -> `actions/web_search.py::web_search()`;
- weather: Gemini `weather_report` -> `actions/weather_report.py::weather_action()`;
- YouTube: Gemini `youtube_video` -> action map in `actions/youtube_video.py`;
- media stop/pause: Gemini or SessionContext reroute -> `actions/media_control.py::media_control()`;
- message send: Gemini `send_message` -> DeviceProfile/confirmation preflight -> `actions/send_message.py::send_message()`.

## 3. Startup Behavior

`main.py::JarvisLive.run()` performs the current startup sequence:

1. starts the optional dashboard and command consumer;
2. creates a fresh Gemini Live client/config on every connection;
3. starts send, microphone, receive, playback, system-monitor, proactive, and optional phone-audio tasks;
4. starts the briefing once per process through `_briefing_sent`.

`JarvisLive._send_startup_briefing()` is a hardcoded two-phase flow:

- phase 1 waits 0.3 seconds and tells Gemini to greet, state the time, and promise that news is being fetched;
- phase 2 calls `_briefing_news_phase()`.

`JarvisLive._briefing_news_phase()` waits 1.5 seconds, then injects a hardcoded instruction to call `web_search(mode='news', query='top world news today')`.

The system monitor is in `actions/system_monitor.py` and `JarvisLive._run_system_monitor()`. Proactive silence checks are in `actions/proactive.py::ProactiveEngine` and `JarvisLive._run_proactive_mode()`.

## 4. News And World-News Trigger

There are two layers:

- Gemini chooses `web_search` and a mode from the prompt/tool description for user requests.
- `actions/web_search.py::web_search()` dispatches `mode='news'` to `_news()`, which races Gemini grounded search and DuckDuckGo news and returns the first valid result.

Startup world news is hardcoded in `main.py`, but execution is prompt-driven: `main.py` injects the exact tool instruction, then Gemini issues the function call.

There is currently no Python guard that limits world news to explicit user requests. There is also an unused `_gemini_headlines()` helper in `actions/web_search.py` that contains another world-news prompt; no caller was found.

## 5. Prompts And Language Connection

- `main.py::_load_system_prompt()` loads `core/prompt.txt`.
- `main.py::_build_config()` prepends current time, long-term memory, a SessionContext snapshot, and DeviceProfile context to that prompt.
- `core/prompt.txt` tells Gemini how to select tools and report action results.
- `core/prompt.txt` documents `[STARTUP_BRIEFING]`, but the current startup phase uses plain text and `[BRIEFING]`. The documented startup tag therefore does not match the actual injected tag.
- Spoken language is chosen from conversation/user memory.
- Fixed UI language is a separate English/Russian system in `core/i18n.py` and `config/settings.json`; it does not automatically control spoken language.

No new visible UI label should be introduced without both English and Russian entries. Tool output spoken in the user's language is separate from fixed UI localization.

## 6. Tool Registration And Execution

`main.py::TOOL_DECLARATIONS` is the Gemini function schema list. `JarvisLive._build_config()` passes it to Gemini Live. `JarvisLive._receive_audio()` receives function calls, invokes `_execute_tool()`, and returns `types.FunctionResponse` objects.

The action implementations are mostly in `actions/*.py`. The declaration list and manual dispatcher are not generated from those modules and can drift. One existing drift is `agent_task`: it is mentioned in `core/prompt.txt` but is not a declared tool.

## 7. SessionContext: Actual Use And Gaps

`core/session_context.py::SessionContext` is instantiated once in `JarvisLive` and keeps five runtime-only actions.

Actual enforcement exists:

- desktop/dashboard text calls `observe_user_text()` and may inject a follow-up-resolution block before Gemini;
- `_execute_tool()` always calls `apply_context_to_tool()` and `resolve_follow_up()` after Gemini selects a tool;
- high/medium confidence media follow-ups can be rerouted to `media_control`;
- high-confidence browser close can be rerouted to `browser_control`;
- an unconfirmed send can be blocked when Gemini already selected `send_message`;
- every non-`save_memory` tool result is recorded.

Gaps:

- ordinary commands are not locally classified;
- voice has no pre-Gemini context wrapper; current transcription is only available during/after Gemini processing;
- voice corrections are attached at `turn_complete`, which can be after a tool call;
- several resolver outcomes are advisory and not executed;
- final assistant speech can overwrite `user_visible_claim` without validation.

## 8. DeviceProfile: Actual Use And Gaps

`JarvisLive.__init__()` calls `ensure_device_profile()`. `_build_config()` injects a summary into Gemini's system instruction. `_execute_tool()` calls `_apply_device_profile_preflight()` before non-memory dispatch.

Concrete preflight branches exist for:

- `browser_control`;
- `open_app`;
- `media_control`;
- `send_message`;
- `screen_process`;
- `computer_settings`, `computer_control`, and `desktop_control`.

Gaps:

- `youtube_video` can open a browser without a DeviceProfile browser preflight;
- microphone startup does not gate on the DeviceProfile audio capability;
- clipboard is not checked as its own capability in the central preflight;
- many actions still perform their own direct platform detection;
- passing DeviceProfile to an action does not itself prove the action consumes it.

These legacy gaps must not be copied into Personal Briefing. Personal Briefing needs no UI automation or device assumption for its local read-only sources.

## 9. Action Result Truthfulness

`core/session_context.py::infer_result_status()` infers `result_status` and `verified` from returned strings. `truthful_claim()` maps them to the allowed Uzbek claim. `_execute_tool()` returns the result, status, verification bit, truthful claim, recent context, and a rule to Gemini.

This is useful but not hard enforcement:

- no typed `ActionResult` contract exists across actions;
- some success strings are accepted without post-state verification;
- the final generated transcript is logged and stored through `note_assistant_claim()` even if it contradicts metadata;
- tests cover the helpers, not the final Gemini claim.

Personal Briefing must therefore return explicit source names and statuses. Missing external adapters must be `not_configured`; the tool must never include placeholder numbers that Gemini could restate as facts.

## 10. Sounddevice Warning Filter

Current filter location: the top of `main.py`, before `import sounddevice`.

Current pattern:

- category: `DeprecationWarning`;
- message prefix: `Setting the shape on a NumPy array has been deprecated...`;
- module: `sounddevice`.

The warning source is `sounddevice.py::_array()` where sounddevice assigns `data.shape = -1, channels`. In the current tree and rebuilt `.venv`, a direct call to that function after full `import main` is suppressed. A callback/thread bypass was not reproducible because Python warning filters are process-global.

Therefore the current evidence does **not** show that the filter is too late or that its current module/message fails. Repeated output can instead come from:

- a process launched before the filter was added;
- a different entrypoint importing `sounddevice` without the main filter;
- a later library changing warning filters;
- old accumulated launcher log output being mistaken for new warnings.

Other project modules import sounddevice directly (`actions/screen_processor.py`, `core/tts.py`, and a local import in `core/platform_adapters/base.py`) and do not install the filter themselves.

Safe cleanup: centralize one idempotent, exact message/category/module filter; install it before every project sounddevice import; reapply it immediately before the long-lived microphone stream; test from a worker thread that the exact sounddevice warning is hidden while an unrelated `DeprecationWarning` remains visible. Do not edit sounddevice, downgrade NumPy, or hide broad warning classes.

## 11. Safe Personal Briefing Integration Point

Use the existing action/tool path rather than a parallel command subsystem:

1. add `actions/personal_briefing.py` with a small source-adapter registry;
2. add one `personal_briefing` declaration to `TOOL_DECLARATIONS`;
3. dispatch it from `JarvisLive._execute_tool()`;
4. add exact routing rules to `core/prompt.txt`;
5. add a narrow intent guard at the top of `_execute_tool()` so a personal phrase mistakenly selected as `web_search/news` is redirected in the same central routing layer;
6. reuse the same action for automatic startup and remove startup's world-news instruction;
7. leave explicit world news on the existing `web_search(mode='news')` path.

For desktop/dashboard text, an internal route hint may be appended to the existing SessionContext payload. Voice cannot safely use a text-only local handler, so the central dispatch guard and tool prompt are required for input parity.

### Source adapter plan

- `local_projects`: configured, read-only, allowlisted project docs and read-only Git metadata only;
- `telegram`: `not_configured` until a real supported API/token/config adapter exists;
- `instagram`: `not_configured` until a real supported API/token/config adapter exists;
- `messenger`: `not_configured` until a real supported API/token/config adapter exists;
- `zerno`: registry placeholder/skeleton returning `not_configured`; no endpoint or token shape should be invented without an API contract.

`foyda`, `zarar`, and `next action` must be evidence-based operational fields:

- `foyda`: verified progress/value from local project state, not invented money;
- `zarar`: verified risks/blockers/dirty state, not invented loss;
- `next action`: the first actionable item from safe project docs;
- financial profit/loss stays `not_configured` until a real financial source exists.

## 12. Files Not To Touch

- `config/api_keys.json`;
- `memory/long_term.json`;
- `.venv/` and the broken-venv backup;
- PyQt6/NumPy dependency versions;
- third-party sounddevice code;
- unrelated reconnect/audio queue logic;
- unrelated launcher logic;
- unrelated browser/media/message implementations.

The pre-existing modified `scripts/launch_jarvis.command` and untracked `.venv_broken_20260711_042005/` belong to the user and must remain outside this change and commit.

## 13. Risks

- Gemini may answer without calling a tool; prompt-only routing is not deterministic.
- A central dispatch guard can correct a wrong tool call, but it cannot intercept a no-tool voice response.
- Startup runs concurrently with immediate user speech, so turns can overlap.
- Local Git/docs can expose only operational state, not social/financial statistics.
- Result truthfulness remains advisory at the final generation boundary.
- Adding a tool requires synchronized declaration, dispatch, prompt, and tests.
- Existing startup documentation claims weather behavior that the current code does not implement.

## 14. Recommended Minimal Patch Plan

1. Implement a pure, narrow briefing-route classifier used by the existing payload/dispatch path.
2. Implement the Personal Briefing action with an allowlisted local adapter and explicit external `not_configured` adapters.
3. Register and dispatch the action in `main.py`.
4. Replace automatic startup phase 2 with the same Personal Briefing action and stop promising/fetching world news on startup.
5. Keep world news in `web_search(mode='news')`, guarded by explicit news intent.
6. Add explicit Personal Briefing/world-news/source-truthfulness rules to `core/prompt.txt`.
7. Centralize and reapply the narrow sounddevice warning filter.
8. Test the pure classifier, central wrong-tool reroute, source registry, safe local docs/Git read, required briefing fields, no fake numbers, warning filter specificity/thread behavior, and existing commands.
9. Update durable project architecture/docs, then run the full required verification.

## Baseline Verification

Before implementation:

```text
.venv/bin/python -m unittest discover -s tests
Ran 32 tests in 0.124s — OK
```

## Implementation Applied After Audit

The audit was completed before runtime changes. The minimal patch then used the integration points identified above:

- `core/briefing_routing.py` now provides a narrow, pure policy for Personal Briefing phrases, named external-statistics requests, explicit world news, and implicit generic-world-news protection.
- desktop/dashboard text keeps the existing Gemini path and receives an internal route hint; voice remains raw Gemini Live audio and is protected by the prompt plus `_execute_tool()` guard.
- `main.py::_execute_tool()` applies the briefing guard before SessionContext/DeviceProfile/action dispatch and records the actual executed tool in the existing FunctionResponse metadata.
- `actions/personal_briefing.py` implements `local_projects` plus Telegram/Instagram/Messenger/Zerno source entries. External defaults are offline `not_configured` adapters with `statistics=None`.
- automatic startup directly collects the same Personal Briefing action, records it in SessionContext, displays it, and sends only the verified report to Gemini under the correct `[STARTUP_BRIEFING]` tag.
- the greeting keeps the pre-existing read-only name/language lookup from long-term memory; the briefing action itself never receives or reads private memory and never uses it for statistics.
- generic world news remains on the existing `actions/web_search.py` news mode only after explicit news intent.
- `core/runtime_warnings.py` centralizes the exact sounddevice NumPy 2.5 filter and it is reapplied just before the microphone callback stream.

The broader audit gaps in SessionContext timing, DeviceProfile coverage, and final-output truthfulness remain documented follow-up debt; they were intentionally not expanded into this focused patch.

## Post-Implementation Verification

```text
.venv/bin/python -m unittest discover -s tests
Ran 59 tests — OK

.venv/bin/python -m py_compile main.py
OK

git diff --check
OK
```

The test suite includes the actual `JarvisLive._execute_tool()` reroute path, startup Personal Briefing without world news, external `not_configured` results, allowlisted local docs/read-only Git, required operational fields, no external fake numbers, multi-command preservation, stale voice-turn cleanup, and worker-thread sounddevice warning specificity.

The full PyQt/Gemini Live GUI launch was requested for manual QA, but the execution approval was unavailable because the Codex runtime had reached its GUI-action usage limit. It was not reported as passed. The live voice/UI checklist remains explicit in `NEXT_STEPS.md`.
