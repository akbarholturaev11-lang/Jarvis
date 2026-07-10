# AI_RESOURCES.md

This file lists important files and folders in MARK XLVIII - AkbarCustom, what they do, their risk level, and when to edit them.

Risk levels:

- LOW = docs, comments, or UI text only
- MEDIUM = feature logic or user-facing behavior
- HIGH = app core, runtime, API, audio, secrets, or tool dispatch

## Core Files And Folders

| Path | Purpose | Risk | Edit When | Do Not Edit When |
| --- | --- | --- | --- | --- |
| `main.py` | Main runtime entry point. Manages Gemini Live session, audio I/O, reconnect flow, tool declarations, and tool dispatch. | HIGH | Fixing session, audio, reconnect, or tool-routing behavior after understanding the flow. | Documentation-only tasks, UI-only changes, or unclear bugs. |
| `ui.py` | PyQt6 HUD/UI layer, visual panels, controls, input widgets, system metrics display, and user interaction surface. | MEDIUM | UI layout, labels, styling, keyboard shortcuts, or display behavior needs adjustment. | Gemini, API, audio, or action behavior is the real issue. |
| `setup.py` | Installs requirements and Playwright browsers for first setup. | HIGH | Setup flow is broken or install process needs a deliberate change. | App runtime behavior needs fixing. Do not change dependency setup casually. |
| `actions/` | Tool/action implementation folder used by `main.py`. | MEDIUM | Adding or fixing a specific assistant capability. | Tool declaration in `main.py` is not understood. |
| `memory/` | Persistent memory package. Stores and formats assistant memory. | HIGH | Memory schema, save/load behavior, trimming, or prompt formatting needs deliberate changes. | User memory data should be preserved. Do not overwrite personal memory. |
| `core/prompt.txt` | Main assistant behavior, language, memory, and tool-routing prompt. | HIGH | Assistant personality, tool policy, or language rules need to change. | Runtime bugs are unrelated to prompt behavior. |
| `config/api_keys.json` | Local secret config containing Gemini API key. | HIGH / SECRET | Only when Akbar explicitly asks to update local secrets. | Almost always. Never commit or expose. |
| `requirements.txt` | Python dependency list. | HIGH | A required package is missing or incompatible, and the reason is clear. | Random upgrades/downgrades or documentation-only work. |

## Action Modules

| Path | Purpose | Risk | Edit When | Do Not Edit When |
| --- | --- | --- | --- | --- |
| `actions/open_app.py` | Opens local applications, websites, or programs by name. | MEDIUM | App launch behavior fails on Mac or needs app-name mapping. | Tool declaration or Gemini routing is the real issue. |
| `actions/computer_control.py` | Handles computer control actions such as keyboard/mouse/window interactions. | HIGH | Low-level control behavior needs fixing after manual test. | Mac permissions are missing or unverified. |
| `actions/browser_control.py` | Controls browser navigation and related web interactions. | MEDIUM | Browser commands fail or need expanded behavior. | The requested task is normal web search. |
| `actions/screen_processor.py` | Captures screen or camera image for vision analysis. | HIGH | Screen/camera capture, permissions, or vision flow needs fixing. | User only needs UI text or prompt changes. |
| `actions/reminder.py` | Creates OS-native reminders or scheduled notifications. | HIGH | Reminder scheduling fails on macOS/Windows/Linux. | Date parsing or assistant wording is the only issue. |
| `actions/send_message.py` | Sends messages through messaging platforms such as WhatsApp or Telegram. | HIGH | Messaging automation is broken and user approves testing. | Recipient/message privacy or platform state is unclear. |
| `actions/web_search.py` | Web search, news, research, price, and compare behavior using Gemini/DDG paths. | MEDIUM | Search results fail, rate-limit handling needs improvement, or mode behavior needs tuning. | Gemini Live session itself is disconnecting. |
| `actions/file_processor.py` | Reads, summarizes, and answers questions about local files. | HIGH | File parsing or summarization behavior is broken. | User files may contain private data and permission is unclear. |
| `actions/code_helper.py` | Code review, debugging, and code-generation helper action. | MEDIUM | Code helper behavior needs refinement. | General app runtime is failing. |
| `actions/dev_agent.py` | Developer task agent for more complex multi-step coding tasks. | HIGH | Agent workflow is explicitly requested or broken. | A direct small patch is enough. |
| `actions/proactive.py` | Proactive check-in logic during user silence. | MEDIUM | Timing, content, or usefulness of proactive checks needs adjustment. | The user wants reactive command behavior only. |
| `actions/personal_briefing.py` | Builds evidence-based Personal Operations Briefing from allowlisted local docs/Git and explicit external adapter statuses. | MEDIUM | Briefing sources, formatting, or verified adapter support changes. | Do not add guessed external statistics or read secret/private files. |

## Additional Useful Files

| Path | Purpose | Risk | Edit When | Do Not Edit When |
| --- | --- | --- | --- | --- |
| `actions/desktop.py` | Desktop/taskbar/window-level operations. | HIGH | Desktop operations need platform-specific fixes. | Mac permissions are not granted. |
| `actions/computer_settings.py` | OS settings actions such as volume, brightness, WiFi, power, and shortcuts. | HIGH | OS settings commands fail on Mac. | The issue is only assistant phrasing. |
| `actions/system_monitor.py` | CPU/RAM/GPU/temperature monitoring and status reporting. | MEDIUM | Metrics are wrong, noisy, or unstable. | UI rendering is the actual problem. |
| `actions/weather_report.py` | Weather lookup behavior. | MEDIUM | Weather command fails or city parsing needs changes. | Web search rate limits are unrelated. |
| `actions/youtube_video.py` | YouTube search/play/summarize/control actions. | MEDIUM | YouTube command behavior fails. | Browser automation is generally broken. |
| `actions/file_controller.py` | File system operations. | HIGH | File operations need a reviewed safety fix. | The request risks deleting or moving user data without confirmation. |
| `core/briefing_routing.py` | Narrow Personal Briefing and explicit world-news routing policy used by the existing dispatcher. | HIGH | Exact briefing/news intent policy needs a tested change. | Do not turn it into a parallel general command system. |
| `core/runtime_warnings.py` | Exact source-specific sounddevice NumPy 2.5 warning filter. | MEDIUM | The known warning signature or import coverage changes. | Do not hide broad warning categories or unrelated errors. |
| `memory/memory_manager.py` | Loads, trims, saves, updates, and formats long-term memory. | HIGH | Memory behavior needs careful schema/runtime changes. | The goal is only documentation. |
| `memory/long_term.json` | Local personal assistant memory. | HIGH / PRIVATE | Only if Akbar explicitly asks. | Never commit, expose, overwrite, or reset casually. |
| `readme.md` | Original project README and capability overview. | LOW | Documentation needs alignment with AkbarCustom. | Runtime behavior needs fixing. |
