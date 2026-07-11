# AkbarCustom Command System Deep Audit

Date audited: 2026-07-11  
Scope: current Jarvis command/action architecture from code only. No feature implementation.

## Executive summary

Jarvis command behavior is hybrid:

1. Gemini Live usually chooses a tool from `main.py::TOOL_DECLARATIONS`.
2. Python manually pre-handles a small number of deterministic typed/dashboard commands before Gemini:
   - `JarvisLive._handle_ui_language_command()`
   - `JarvisLive._handle_device_profile_local_command()`
3. Python also guards or reroutes some Gemini tool calls inside `JarvisLive._execute_tool()`:
   - `core.briefing_routing.apply_briefing_route()`
   - `core.session_context.SessionContext.apply_context_to_tool()`
   - `core.session_context.SessionContext.resolve_follow_up()`
   - `JarvisLive._apply_device_profile_preflight()`
4. Tool implementations live mainly in `actions/*.py`.

The system is not a fixed phrase command list. Users can speak/type natural language, Gemini maps intent to tool calls, and Python dispatches/guards the selected tool. The main weak point is that final spoken claims still depend on Gemini obeying the prompt and the tool response metadata; Python records truthfulness metadata, but it does not mechanically rewrite Gemini's final answer.

## Command flow diagram

```text
Desktop voice
  sounddevice.InputStream callback
  -> main.py::JarvisLive._listen_audio()
  -> JarvisLive._enqueue_outgoing_audio({"data": ..., "mime_type": "audio/pcm"})
  -> JarvisLive._send_realtime()
  -> Gemini Live session.send_realtime_input(media=...)
  -> Gemini Live transcribes / reasons / may emit tool_call
  -> JarvisLive._receive_audio()
  -> JarvisLive._execute_tool(fc, user_text=current_user_text)
  -> actions/* function or internal tool
  -> types.FunctionResponse(...)
  -> session.send_tool_response(function_responses=...)
  -> Gemini speaks final answer
  -> JarvisLive._play_audio()
```

```text
Desktop typed text
  ui.py::MainWindow._send()
  -> JarvisUI.on_text_command callback
  -> main.py::JarvisLive._on_text_command()
  -> local pre-handlers for UI language / DeviceProfile
  -> SessionContext.observe_user_text()
  -> SessionContext.build_user_turn_context()
  -> core.briefing_routing.build_briefing_route_hint()
  -> Gemini Live session.send_client_content(...)
  -> tool_call path above
```

```text
Remote dashboard text
  dashboard/server.py::DashboardServer._build_app()
    /api/command or websocket /ws type="command"
  -> DashboardServer._command_queue
  -> main.py::JarvisLive._process_dashboard_commands()
  -> same local pre-handlers / SessionContext / briefing hint
  -> Gemini Live session.send_client_content(...)
  -> tool_call path above
```

```text
Phone microphone
  dashboard/server.py websocket /ws/phone-audio
  -> DashboardServer._phone_audio_queue
  -> main.py::JarvisLive._relay_phone_audio()
  -> JarvisLive._enqueue_outgoing_audio(...)
  -> JarvisLive._send_realtime()
  -> Gemini Live audio path
```

## 1. User input flow

### Voice input

Voice enters through `main.py::JarvisLive._listen_audio(session_generation)`.

Important code path:

- `sounddevice.InputStream(...)` opens the microphone.
- The nested `callback(indata, frames, time_info, status)` converts microphone frames to bytes.
- The callback calls `JarvisLive._enqueue_outgoing_audio({"data": data, "mime_type": "audio/pcm"}, session_generation)`.
- `JarvisLive._send_realtime()` reads `self.out_queue` and sends each chunk to Gemini with `session.send_realtime_input(media=msg)`.

Transcribed user speech is received in `main.py::JarvisLive._receive_audio()`:

- `response.server_content.input_transcription.text` is accumulated into `in_buf`.
- On `sc.turn_complete`, `full_in` is written to UI log and passed to `self.session_context.observe_user_text(full_in)`.
- If Gemini emits a tool call before turn completion, `_receive_audio()` uses `current_user_text = " ".join(in_buf).strip() or self._active_user_text`.

### Text input

Desktop text enters through `ui.py::MainWindow._send()`.

Flow:

- `MainWindow._send()` reads `self._input.text()`.
- It logs `You: ...`.
- It starts a thread calling `self.on_text_command(txt)`.
- `JarvisUI.on_text_command` is set in `main.py::JarvisLive.__init__()` to `self._on_text_command`.
- `main.py::JarvisLive._on_text_command(text)` is the first runtime command receiver for desktop typed text.

Remote dashboard text enters through `dashboard/server.py`:

- `DashboardServer._build_app()` defines `@app.post("/api/command")`.
- It decrypts or reads `body["text"]`, then `await self._command_queue.put(text)`.
- Websocket `/ws` also accepts JSON with `type == "command"` and puts text into `_command_queue`.
- `main.py::JarvisLive._process_dashboard_commands()` consumes `_dashboard._command_queue`.

### File-upload prompt input

When a file is dropped in the desktop UI, `ui.py::MainWindow._on_file_selected()` sends an internal text command:

```text
[FILE_UPLOADED] path=... | name=... | type=... | size=...
```

That goes through `on_text_command`, then the same Gemini path.

### How messages are sent to Gemini

Text and internal prompts are sent using:

- `session.send_client_content(turns={"parts": [{"text": payload_text}]}, turn_complete=True)`

Realtime audio is sent using:

- `session.send_realtime_input(media=msg)`

Tool results are sent back using:

- `session.send_tool_response(function_responses=fn_responses)`

## 2. Prompt role

### Main system prompt location

The main system prompt is `core/prompt.txt`.

It is loaded by:

- `main.py::_load_system_prompt()`

It is inserted into Gemini Live config by:

- `main.py::JarvisLive._build_config()`

`_build_config()` builds `system_instruction` from:

1. current date/time block;
2. long-term memory from `memory.memory_manager.format_memory_for_prompt()`;
3. `SessionContext.build_prompt_context()`;
4. `core.device_profile.format_device_profile_for_prompt()`;
5. `core/prompt.txt`.

### What `core/prompt.txt` tells Gemini

`core/prompt.txt` gives high-level assistant behavior and routing rules. It explicitly says:

- call `screen_process` once for vision;
- store critical memory via `save_memory`;
- only call `shutdown_jarvis` for explicit session termination;
- inspect recent session action context before vague follow-ups like `o'chir`, `to'xtat`, `yubor`, `yana qil`, `nima qilding`;
- consult DeviceProfile before app/browser/media/message/screen/camera/microphone/UI automation;
- route media stop/pause follow-ups to `media_control`;
- route `men uydaman`, `uydaman`, `ishga qaytdim`, `loyihalarimni tekshir`, `statistikani ayt`, and external stats questions to `personal_briefing`;
- use `web_search mode='news'` only for explicit world news requests;
- use `device_profile` for scan/query commands.

### Does the prompt describe available actions?

Yes. The prompt has a `TOOL ROUTING:` section naming tools such as:

- `computer_settings`
- `media_control`
- `agent_task` in the prompt text, although the registered tool is `dev_agent`, not `agent_task`
- `system_status`
- `personal_briefing`
- `web_search`
- `set_ui_language`

The actual executable schemas are not in `core/prompt.txt`; they are in `main.py::TOOL_DECLARATIONS`.

### Does the prompt say "if user means X, do Y"?

Yes. Examples:

- vague media stop/pause -> `media_control`;
- `men uydaman` / `statistikani ayt` -> `personal_briefing`;
- explicit world news -> `web_search mode='news'`;
- device scan/query phrases -> `device_profile`.

### Is command behavior mostly prompt-driven?

Tool selection is mostly Gemini + prompt + `TOOL_DECLARATIONS`, but important safety/routing behavior is code-driven:

- `core.briefing_routing.apply_briefing_route()` can override Gemini's selected tool.
- `SessionContext.resolve_follow_up()` can reroute vague media/browser/message follow-ups.
- `JarvisLive._apply_device_profile_preflight()` can block or fill parameters before execution.

So the real architecture is not prompt-only.

## 3. Tool/action selection

### Does Gemini select tools/function calls?

Yes. Gemini receives tool declarations in:

- `main.py::JarvisLive._build_config()`
- `types.LiveConnectConfig(..., tools=[{"function_declarations": TOOL_DECLARATIONS}], ...)`

Gemini emits tool calls received at:

- `main.py::JarvisLive._receive_audio()`
- `if response.tool_call:`
- `for fc in response.tool_call.function_calls:`
- `fr = await self._execute_tool(fc, user_text=current_user_text)`

### Does Python manually parse commands?

Yes, but only for narrow cases:

- `JarvisLive._handle_ui_language_command()` uses `core.i18n.detect_ui_language_command()`.
- `JarvisLive._handle_device_profile_local_command()` uses `is_device_profile_refresh_request()` and `is_device_profile_query_request()`.
- `core.briefing_routing.resolve_briefing_route()` recognizes personal briefing, explicit world news, and external statistics source requests.
- `core.session_context.SessionContext.resolve_follow_up()` recognizes vague follow-ups and corrections.

### Is it hybrid?

Yes. Normal natural language intent is Gemini-selected. Python applies deterministic routing and preflight guardrails before execution.

### Where are tools registered?

Tools are registered in:

- `main.py::TOOL_DECLARATIONS`

The declarations are passed to Gemini in:

- `main.py::JarvisLive._build_config()`

### Where are tool schemas/descriptions defined?

All Gemini-visible tool schemas/descriptions are inline dictionaries in:

- `main.py::TOOL_DECLARATIONS`

### Where does Gemini decide which action to call?

Gemini decides inside the Live API session using:

- the `system_instruction` from `_build_config()`;
- the `tools` declarations from `_build_config()`;
- user audio/text sent through `send_realtime_input()` or `send_client_content()`.

Python receives the result as `response.tool_call.function_calls` in `_receive_audio()`.

## 4. Tool execution

### Central executor

The central executor is:

- `main.py::JarvisLive._execute_tool(self, fc, user_text: str = "")`

### What input does it receive?

It receives `fc`, a Gemini function call object with:

- `fc.name`
- `fc.args`
- `fc.id`

It also receives `user_text`, normally the current transcribed or typed user turn.

### Tool call data format

`_execute_tool()` converts arguments with:

```python
response_name = fc.name
original_args = dict(fc.args or {})
```

Then it may transform:

```python
name, routed_args, route_note = apply_briefing_route(user_text, response_name, original_args)
args, context_note = self.session_context.apply_context_to_tool(user_text, name, routed_args)
```

### Execution steps inside `_execute_tool()`

1. Read Gemini-selected `response_name` and `original_args`.
2. Apply `core.briefing_routing.apply_briefing_route()`.
3. Apply `SessionContext.apply_context_to_tool()`.
4. Resolve vague follow-up with `SessionContext.resolve_follow_up()`.
5. Possibly reroute to `media_control` or `browser_control`, or block with confirmation text.
6. Apply `JarvisLive._apply_device_profile_preflight()`.
7. Dispatch via `if/elif name == ...`.
8. Run action functions, usually with `loop.run_in_executor(...)`.
9. Infer status with `core.session_context.infer_result_status(name, result)`.
10. Create truthful claim with `truthful_claim(status, verified)`.
11. Record action in `self.session_context.record_action(...)`.
12. Return `types.FunctionResponse(...)` to Gemini.

### How it calls `actions/*`

Examples from `_execute_tool()`:

- `open_app` -> `actions.open_app.open_app(parameters=args, response=None, player=self.ui)`
- `browser_control` -> `actions.browser_control.browser_control(parameters=args, player=self.ui)`
- `send_message` -> `actions.send_message.send_message(parameters=args, response=None, player=self.ui, session_memory=self.session_context)`
- `media_control` -> `actions.media_control.media_control(parameters=args, response=None, player=self.ui, session_memory=self.session_context, device_profile=self.device_profile)`
- `file_processor` -> `actions.file_processor.file_processor(parameters=args, player=self.ui, speak=self.speak)`
- `personal_briefing` -> `actions.personal_briefing.personal_briefing(parameters=args, player=None, project_root=BASE_DIR)`

### How result returns to Gemini/UI

The executor returns:

```python
types.FunctionResponse(
    id=fc.id,
    name=response_name,
    response={
        "result": result,
        "result_status": status,
        "verified": verified,
        "truthful_user_claim": claim,
        "recent_action_context": self.session_context.build_prompt_context(),
        "context_applied": context_note,
        "actual_tool_executed": name,
        "followup_resolution": followup_resolution,
        "assistant_rule": "...",
    }
)
```

Important detail: the returned `name` is `response_name`, the original Gemini-selected function name, even if Python rerouted actual execution to another `name`. The real executed tool is included in response metadata as `actual_tool_executed`.

UI updates happen in individual branches, for example:

- `web_search` calls `self.ui.show_content(...)` for non-empty results.
- `personal_briefing` calls `self.ui.show_content("PERSONAL BRIEFING", result)`.
- many action functions call `player.write_log(...)`.

## 5. Existing executable capabilities

This table lists Gemini-registered tools that `_execute_tool()` can execute.

| Tool/action name | File/function | What it does | Example phrases | Required parameters | Platform dependency | Verification | Known weaknesses |
|---|---|---|---|---|---|---|---|
| `open_app` | `actions/open_app.py::open_app()` | Opens an app using OS-specific launchers. | "open Safari", "Telegramni och" | `app_name` | Windows/macOS/Linux | Partial; returns `Opened ...` after launcher success | `open_app.py` uses process launch success, not full UI verification; DeviceProfile preflight can block unknown apps |
| `web_search` | `actions/web_search.py::web_search()` | Search/news/research/price/compare through search backends and Gemini headlines. | "search X", "dunyo yangiliklari", "price of X" | `query` unless `items` compare | Network-dependent | Usually uncertain by status heuristic unless failure text appears | Search/news can fail or rate-limit; result truth depends on backend |
| `system_status` | `actions/system_monitor.py::get_system_status()` | CPU/RAM/GPU/temp/uptime/process snapshot. | "CPU qanday?", "RAM usage" | none | Uses `psutil`; temp/GPU OS dependent | Returned dict; status heuristic may mark uncertain because it is not a success phrase | Metrics may omit GPU/temp |
| `personal_briefing` | `actions/personal_briefing.py::personal_briefing()` | Reads allowlisted project docs + read-only Git metadata; reports external source adapters as `not_configured`. | "men uydaman", "statistikani ayt", "Telegram statistikasi" | optional `sources`, `scope` | Local filesystem + Git | `infer_result_status()` marks `[PERSONAL_OPERATIONS_BRIEFING]` as success/verified unless failed | External stats are placeholders until real adapters exist |
| `set_ui_language` | `core/i18n.py::change_ui_language()` via `_execute_tool()` | Changes UI language setting to `en` or `ru`. | "UI ni English qil", "rus qil" | `language` | Local config write | Verified if result text matches expected language-change text | Full visible UI needs restart |
| `device_profile` | `main.py::JarvisLive._device_profile_tool()` using `core/device_profile.py` | Summarize/query/refresh DeviceProfile. | "qaysi qurilmada ishlayapsan?", "Telegram bormi?", "qurilmani qayta tekshir" | `action` | Platform adapter dependent | Some query/refresh messages match success starts | Profile can be stale until refresh; unknown stays unknown |
| `weather_report` | `actions/weather_report.py::weather_action()` | Opens Google weather search in browser. | "weather in Tashkent" | `city` | Browser/webbrowser | Not strongly verified; browser open can return true | Does not parse weather itself |
| `send_message` | `actions/send_message.py::send_message()` | Drafts or attempts messages via WhatsApp/Telegram/Signal/Discord/Instagram/Messenger desktop/web automation. | "Ali ga Telegramdan xabar yubor" | `receiver`, `message_text`, `platform` | GUI + PyAutoGUI + app/browser | Intentionally not verified unless result contains `verified sent`; current handlers return draft/attempt not verified | Contact/chat/delivery not verified; DeviceProfile requires confirmation and UI automation |
| `reminder` | `actions/reminder.py::reminder()` | Schedules system reminder. | "remind me tomorrow at 9" | `date`, `time`, `message` | Windows Task Scheduler / macOS launchd / Linux systemd-run or at | Returns `Reminder set ...`; status heuristic recognizes `reminder set` | Date parsing requires exact `YYYY-MM-DD` and `HH:MM` from Gemini |
| `youtube_video` | `actions/youtube_video.py::youtube_video()` | Play/search YouTube, summarize transcript, info, trending. | "play relaxing music on YouTube", "summarize this video" | none globally; action-specific | Network + browser + optional transcript package | Play opens URL/search but not playback verification | Scraping/transcript can fail; playback success not verified |
| `media_control` | `actions/media_control.py::media_control()` | Safe pause/play-pause, especially macOS; can verify browser media for supported browsers. | "to'xtat", "o'chir", "pause music" after media context | optional `action`, `target_app`, `target_context` | macOS osascript/PyAutoGUI or platform adapter | Verified only when browser JS verifier confirms no playback; otherwise uncertain | Does not close/kill; generic system pause often cannot verify |
| `screen_process` | Implemented inline in `main.py::_execute_tool()` using `actions.screen_processor._capture_screen()` / `_capture_camera()` | Captures screen/camera, then injects image into Gemini. | "ekranga qaragin", "camera ni ko'r" | `text`; `angle` optional | Screen/camera permissions, `mss`, `cv2` | Capture success is real; final visual answer comes from Gemini image turn | One-call cooldown; permissions can block |
| `close_camera` | `main.py::_execute_tool()` -> `self.ui.stop_camera_stream()` | Closes live camera preview. | "close camera", "kamerani yop" | none | UI camera stream | Returns `Camera closed.`; status heuristic recognizes it | Only closes UI preview, not necessarily OS camera state if external failure |
| `computer_settings` | `actions/computer_settings.py::computer_settings()` | Single OS actions: volume, brightness, close/window/tab, shortcuts, WiFi, restart/shutdown, etc. | "volume up", "tabni yop", "restart computer" | optional `action`, `description`, `value` | Strongly OS/GUI dependent | Many actions return `Done: ...`, typed/pressed/scrolled messages | Broad and risky; dangerous restart/shutdown require confirmed flag; DeviceProfile gates UI automation |
| `browser_control` | `actions/browser_control.py::browser_control()` | Browser sessions: go_to, search, click, type, scroll, screenshot, tabs, close. | "open site in Chrome", "click login" | `action` | Playwright/browser availability | Many methods return explicit action messages; close/list are direct | Browser startup can fail; DeviceProfile chooses browser if omitted |
| `file_controller` | `actions/file_controller.py::file_controller()` | Files/folders: list/create/delete/move/copy/rename/read/write/find/largest/disk usage/desktop organize/info. | "list Downloads", "rename file", "delete X" | `action` | Filesystem | Direct filesystem result messages | Mutating actions can be destructive; safe-path policy is inside action file, but still high risk |
| `desktop_control` | `actions/desktop.py::desktop_control()` | Wallpaper, desktop organize/clean/list/stats, or generated desktop task. | "organize desktop", "set wallpaper" | `action` | Desktop OS dependent; may use generated code | Mixed; stats/list direct, task generated code uncertain | `task` asks Gemini to generate desktop code; higher risk |
| `code_helper` | `actions/code_helper.py::code_helper()` | Write/edit/explain/run/build/optimize/screen_debug code. | "write a Python script", "run this file" | `action` | Filesystem/subprocess; language dependent | File/run results direct but not globally verified by heuristic | Can execute code; path/project safety depends on helper implementation |
| `dev_agent` | `actions/dev_agent.py::dev_agent()` | Builds multi-file projects, may install deps/open VSCode/run/fix. | "build me a small app" | `description` | Filesystem/subprocess/package tools | Mixed; returns build/run result | Very broad; can install dependencies from generated project flow |
| `computer_control` | `actions/computer_control.py::computer_control()` | Direct mouse/keyboard/clipboard/screenshot/screen_find automation. | "click here", "type this", "press enter" | `action` | PyAutoGUI/GUI/screen permissions | Often returns action messages | Coordinates and UI state can be wrong; DeviceProfile gates UI automation |
| `game_updater` | `actions/game_updater.py::game_updater()` | Steam/Epic list/install/update/status/schedule. | "update Steam games", "install game X" | optional `action`, `platform`, `game_name` | Steam/Epic availability, OS | Mixed direct messages | Steam/Epic automation brittle; install/download can be long-running |
| `flight_finder` | `actions/flight_finder.py::flight_finder()` | Searches Google Flights via browser/scrape and formats options. | "find flights from A to B" | `origin`, `destination`, `date` | Network/browser/Gemini parse | Search result text direct; not booking verification | Google Flights scraping can break |
| `shutdown_jarvis` | `main.py::_execute_tool()` internal branch | Speaks goodbye and exits process with `os._exit(0)` after 1s. | "Jarvis shutdown", "goodbye" | none | Local process | Direct process exit | Dangerous if Gemini misroutes vague "stop"; prompt says only explicit termination |
| `file_processor` | `actions/file_processor.py::file_processor()` | Process dropped/uploaded file: image/PDF/doc/text/data/JSON/code/audio/video/archive/PPTX. | "summarize this PDF", "convert this image" | `file_path` usually filled from UI current file; action optional | File type dependencies, Gemini for some processing | Direct output/file result; heuristic mixed | Requires existing file; some operations depend on optional libraries |
| `save_memory` | `memory/memory_manager.py::update_memory()` via `_execute_tool()` | Saves long-term personal fact silently. | User reveals name/language/preferences | `category`, `key`, `value` | Local memory file | Returns silent ok, not recorded like normal tool | Can store too much if Gemini overuses it; prompt says critical facts only |

### Action files present but not Gemini-registered as top-level tools

These files exist in `actions/` but are not separate names in `main.py::TOOL_DECLARATIONS`:

- `actions/proactive.py`: used by `main.py::JarvisLive._run_proactive_mode()`, not a user tool.
- `actions/system_monitor.py::SystemMonitor`: used by `_run_system_monitor()` for background alerts; `get_system_status()` is registered as `system_status`.
- `actions/screen_processor.py::screen_process()`: exists, but the registered `screen_process` tool is executed inline in `main.py` using `_capture_screen()` / `_capture_camera()`.

## 6. Natural language command behavior

### Is there a fixed command list?

No fixed user-facing phrase list exists. There is a fixed Gemini tool list in `main.py::TOOL_DECLARATIONS`, but users can speak naturally. Gemini maps natural language to function calls using the prompt and tool schema descriptions.

### Or can user speak naturally and Gemini maps meaning to tools?

Yes. Natural language is expected. Examples:

- "open Telegram" -> likely `open_app(app_name="Telegram")`;
- "what is on my screen?" -> `screen_process(angle="screen", text=...)`;
- "play X on YouTube" -> `youtube_video(action="play", query="X")`;
- "men uydaman" -> `personal_briefing`.

### What happens if user asks for something similar to an existing tool?

Gemini may choose the closest declared tool. Then Python may:

- execute it directly;
- enrich parameters from SessionContext;
- block it through DeviceProfile preflight;
- reroute it if it is a protected briefing/news/follow-up case.

If Gemini picks an unsupported action inside a valid tool, the action usually returns an unknown-action message, for example:

- `browser_control()` returns `Unknown browser action: ...`;
- `computer_control()` returns `Unknown action: ...`;
- `file_controller()` returns `Unknown action: ...`.

### What happens if no tool exists?

Gemini can answer conversationally without a tool. If it emits a tool name that `_execute_tool()` does not recognize, `_execute_tool()` returns:

```text
Unknown tool: <name>
```

### Can Gemini falsely claim it did something if no tool exists?

Risk exists. The prompt says not to claim success without verified tool results, and `_execute_tool()` returns `result_status`, `verified`, and `truthful_user_claim`. But final speech is generated by Gemini. Python does not currently force-rewrite the final spoken response. `PROJECT_MEMORY.md` also notes: "Final Gemini speech truthfulness is still guided by tool metadata rather than mechanically intercepted."

## 7. Ambiguous command behavior

| Phrase | Current likely route | Code/prompt reason | Real tool? | Can verify? |
|---|---|---|---|---|
| `to'xtat` | If recent media context exists: `media_control(action="pause")`. If recent browser context and Gemini chose close, low-confidence cases can be blocked. If phrase includes "Jarvis" and shutdown intent, Gemini may choose `shutdown_jarvis`. | `core/prompt.txt` Follow-up routing; `SessionContext._is_media_stop_text()`; `_execute_tool()` reroutes to `media_control` when `resolved_intent in {"media_pause", "media_stop"}`. | Yes: `media_control`; possibly `shutdown_jarvis` only if explicit assistant termination. | Media verified only for supported browser JS verifier; otherwise uncertain. |
| `o'chir` | Same as `to'xtat` for recent media/audio; otherwise clarification/block if low confidence. | `_is_media_stop_text()` includes `o'chir`; prompt says media stop/pause follow-ups mean `media_control`, not close/settings. | Yes: `media_control`. | Same as above. |
| `yop` | If recent browser/page context: `browser_control(action="close_tab")`; if low confidence: confirmation question. Gemini may also choose `computer_settings` close actions, but `_execute_tool()` blocks vague low-confidence close for relevant tool names. | `SessionContext._is_close_text()` and `_record_is_browser_context()`; `_execute_tool()` reroutes `resolved_intent == "browser_close"` to `browser_control`. | Yes: `browser_control`; also `computer_settings` has close actions. | Browser close direct result; not full visual verification. |
| `yubor` | If recent message context: `send_message` confirmation flow, usually blocked until explicit confirmation. Direct full message requests still go through `send_message`, but DeviceProfile requires installed app, receiver, confirmation, and UI automation. | `_is_send_text()`; `resolve_messaging_route()` requires `confirmed`; `_execute_tool()` sets `preflight_result = Confirmation needed...` for unconfirmed follow-up. | Yes: `send_message`. | Current action does not verify contact/chat/delivery; result is uncertain even when attempted. |
| `yana qil` | SessionContext marks it vague, but there is no dedicated repeat executor. Likely Gemini gets recent context in prompt and may choose a similar tool again. | `_VAGUE_PATTERNS` includes `yana qil`; `resolve_follow_up()` defaults to `repeat_context` when no specific branch matches. | No dedicated `repeat` tool. | Depends on whatever tool Gemini chooses. |
| `nima qilding?` | Gemini should answer from recent SessionContext. No direct tool is needed. | `_is_what_done_text()` returns `action_status`; `build_user_turn_context()` injects recent actions into prompt. | No dedicated status tool; SessionContext data exists. | It can report previous `result_status`/`verified` from context, but final wording is Gemini-generated. |
| `men uydaman` | `personal_briefing`. | `core.briefing_routing._PERSONAL_PATTERNS`; `build_briefing_route_hint()` for text/dashboard; `apply_briefing_route()` in `_execute_tool()` for all tool calls including voice. Prompt also says MUST call `personal_briefing`. | Yes: `personal_briefing`. | Yes for local/doc/Git collection status; external sources are verified `not_configured`, not real stats. |
| `yangiliklarni ayt` | Likely `web_search`, possibly `mode="news"` if Gemini treats it as news. It is not a Personal Briefing phrase. | `core/prompt.txt` says world news uses `web_search mode='news'` for explicit news. `apply_briefing_route()` only hardcodes `dunyo yangiliklari`, `world news`, `latest news`, and Russian equivalents. `_EXPLICIT_NEWS_TERM_PATTERN` includes `yangilik\w*`, so an explicit news mode selected by Gemini would not be downgraded. | Yes: `web_search`. | Search result not strongly verified by `infer_result_status()`; backend can fail/rate-limit. |
| `statistikani ayt` | `personal_briefing`. | Exact regex in `_PERSONAL_PATTERNS`: `\bstatistikani\s+ayt\b`. Prompt and tool declaration also require `personal_briefing`. | Yes: `personal_briefing`. | Yes for source status; external stats are `not_configured` unless real adapter exists. |

## 8. Startup behavior

### What runs automatically when app starts?

`main.py::main()` creates `JarvisUI`, starts a background `runner()` thread, waits for API key, creates `JarvisLive`, and runs `JarvisLive.run()`.

Inside `JarvisLive.run()`:

- optional dashboard server starts via `DashboardServer`;
- Gemini Live connects;
- session tasks start:
  - `_send_realtime()`
  - `_listen_audio()`
  - `_receive_audio()`
  - `_play_audio()`
  - `_run_system_monitor()`
  - `_run_proactive_mode()`
  - `_relay_phone_audio()` if dashboard exists
- once per process, `_send_startup_briefing()` starts.

### Where is startup briefing/news triggered?

Startup briefing is triggered in:

- `main.py::JarvisLive.run()`
- branch: `if not self._briefing_sent: ... self._create_session_task(tg, self._send_startup_briefing(...), "live-briefing")`

The actual local briefing data is collected in:

- `main.py::JarvisLive._briefing_personal_phase()`
- calls `actions.personal_briefing.personal_briefing(...)` directly via `asyncio.to_thread(...)`.

### Is "top world news today" hardcoded?

`"top world news today"` exists in `core.briefing_routing.resolve_briefing_route()` only for explicit world-news route:

```python
{"tool_name": "web_search", "arguments": {"mode": "news", "query": "top world news today"}}
```

Startup does not use that. Startup uses `personal_briefing_action()` with `DEFAULT_PERSONAL_SOURCES`.

### Is startup Gemini prompt-driven?

Startup is two-phase:

1. `_send_startup_briefing()` sends Gemini a short no-tool greeting prompt.
2. `_briefing_personal_phase()` directly runs `personal_briefing_action()` in Python, then sends Gemini a `[STARTUP_BRIEFING]` instruction containing the already-collected report and explicitly says not to call web search or any other tool.

So startup data collection is code-driven. Gemini only speaks/summarizes it.

## 9. SessionContext and DeviceProfile

### SessionContext

Created in:

- `main.py::JarvisLive.__init__()`
- `self.session_context = SessionContext()`

Defined in:

- `core/session_context.py::SessionContext`

Updated in:

- `JarvisLive._on_text_command()` via `self.session_context.observe_user_text(text)`.
- `JarvisLive._receive_audio()` on turn complete via `observe_user_text(full_in)`.
- `JarvisLive._execute_tool()` after every non-`save_memory` tool result via `record_action(...)`.
- `JarvisLive._receive_audio()` records assistant final text with `note_assistant_claim(full_out)`.

Used before tool selection?

- For typed/dashboard text, yes partially: `build_user_turn_context()` injects follow-up context into the user payload sent to Gemini.
- For voice, current session context is in the system instruction from `_build_config()`; fresh voice-turn-specific context is not prepended before Gemini selects a tool.

Used during tool execution?

- Yes. `_execute_tool()` calls:
  - `apply_context_to_tool()`
  - `resolve_follow_up()`
  - reroutes/block logic based on the resolution.

Influence on Gemini's choice:

- It influences Gemini through prompt context.
- It also influences actual execution after Gemini selection by modifying/rerouting/blocking tool calls.

### DeviceProfile

Created/loaded in:

- `main.py::JarvisLive.__init__()`
- `self.device_profile = ensure_device_profile(BASE_DIR)`

Defined/managed in:

- `core/device_profile.py`
- `core/environment_discovery.py`
- `core/platform_adapters/base.py`
- `core/platform_adapters/macos.py`
- `core/platform_adapters/windows.py`
- `core/platform_adapters/linux.py`

Updated in:

- `core.device_profile.ensure_device_profile()`: loads or creates `config/device_profile.json`.
- `core.device_profile.refresh_device_profile()`: refreshes via environment discovery and saves JSON.
- `JarvisLive._device_profile_tool()` for Gemini tool action `refresh`.
- `JarvisLive._handle_device_profile_local_command()` for typed/dashboard local pre-handler refresh/query.

Used before tool selection?

- Yes, as prompt context: `_build_config()` appends `format_device_profile_for_prompt(self.device_profile)` to Gemini system instruction.
- Typed/dashboard device-profile query/refresh commands are handled locally before Gemini.

Used during tool execution?

- Yes. `JarvisLive._apply_device_profile_preflight()` checks:
  - `browser_control` -> `resolve_browser_route()`
  - `open_app` -> `resolve_app_route()`
  - `media_control` -> `resolve_media_route()`
  - `send_message` -> `resolve_messaging_route()` + `check_permission_gate("ui_automation")`
  - `screen_process` -> `check_permission_gate("screen_capture" or "camera")`
  - `computer_settings`, `computer_control`, `desktop_control` -> `check_permission_gate("ui_automation")`

Influence on Gemini's choice:

- It informs Gemini via system prompt.
- It cannot force Gemini's initial selection, but executor preflight can block or normalize execution.

## 10. Correct way to add a new command/capability

### Do not add real capabilities to the prompt only

Prompt-only is correct only for behavior guidance. If Jarvis must actually do something, add a real action/tool. Otherwise Gemini may claim or plan an action that Python cannot execute.

### Preferred path for a new executable capability

1. Add or extend an action implementation in `actions/<capability>.py`.
2. Import it in `main.py`.
3. Add a schema in `main.py::TOOL_DECLARATIONS`.
4. Add a dispatch branch in `main.py::JarvisLive._execute_tool()`.
5. Return explicit result strings that `infer_result_status()` can classify, or update `core/session_context.py::infer_result_status()` with narrow patterns.
6. If platform-sensitive, add DeviceProfile checks through `core/device_profile.py` and `_apply_device_profile_preflight()`.
7. If it creates ambiguous follow-ups, add general SessionContext logic in `core/session_context.py`; do not add one-off phrase hacks.
8. Add prompt routing guidance in `core/prompt.txt` only after the real tool exists.
9. If visible UI text is added, update bilingual English/Russian localization via `core/i18n.py`.
10. Run verification.

### When to use a manual pre-handler

Use a manual pre-handler only for deterministic local commands that should not spend a Gemini turn and are safe to answer directly, like the existing:

- UI language command;
- DeviceProfile query/refresh command.

Do not build a parallel natural-language command system. The current architecture expects Gemini tool selection plus central `_execute_tool()` guardrails.

### Files likely touched for future commands

- `actions/<new_action>.py`
- `main.py`:
  - import
  - `TOOL_DECLARATIONS`
  - `_execute_tool()` branch
  - possibly `_describe_tool_intent()`
  - possibly `_apply_device_profile_preflight()`
- `core/prompt.txt` for routing guidance
- `core/session_context.py` for follow-up context and truthful result classification
- `core/device_profile.py` and `core/platform_adapters/*` for platform-sensitive capabilities
- `core/i18n.py` only for visible UI text

### Files that should not be touched unless explicitly required

- `.venv/`
- `config/api_keys.json`
- `memory/long_term.json`
- dependencies / `requirements.txt`
- broad architecture files unrelated to the command
- `core/briefing_routing.py` unless the change is specifically about the narrow Personal Briefing / explicit world-news policy

## Risks and weak points

1. Final spoken truthfulness is not mechanically enforced. `_execute_tool()` returns truthful metadata, but Gemini generates the final spoken response.
2. `infer_result_status()` is string-pattern based. Tools with valid results that do not start with recognized success phrases may be marked uncertain.
3. Some tools are broad and risky:
   - `computer_settings`
   - `computer_control`
   - `file_controller`
   - `desktop_control task`
   - `dev_agent`
4. `send_message` cannot currently verify recipient/chat/delivery. It correctly returns draft/attempt/uncertain language, but final Gemini wording must obey that.
5. `open_app` verifies launcher success, not necessarily that the user-visible app is ready.
6. `youtube_video play` opens a YouTube URL/search but does not verify playback.
7. DeviceProfile can be stale until refreshed. Executor preflight helps, but Gemini may still initially select a bad tool.
8. Voice commands do not get the same per-turn `build_briefing_route_hint()` prepended before tool selection; they rely on system prompt and `_execute_tool()` route guard after Gemini selects a tool.
9. `core/prompt.txt` references `agent_task`, but the registered tool is `dev_agent`. That mismatch can confuse tool selection, although Gemini can only call registered declarations.
10. Some action modules use generated code or external web scraping. These paths are inherently less deterministic than direct API/tool calls.

## Verification commands for this audit

Required by task after creating this file:

```bash
.venv/bin/python -m py_compile main.py
git diff --check
git status
```
