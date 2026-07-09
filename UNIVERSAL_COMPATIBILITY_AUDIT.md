# Universal Compatibility Audit

Date: 2026-07-10

Scope: MARK XLVIII - AkbarCustom runtime actions, routing, and environment-sensitive modules.

Legend:

- Cross-platform OK: works without OS-specific assumptions or has safe cross-platform fallback.
- Partial: supports some platforms but not all or cannot verify result.
- Platform-specific: intentionally OS-specific.
- Permission-dependent: can fail because of OS permissions.
- Verification risk: action can affect real apps/messages without proving success.

| File/module | Current behavior | Platform status | Risk | Verification ability | Recommended fix | Priority |
|---|---|---:|---|---|---|---:|
| `main.py` tool dispatch | Receives Gemini tool calls, applies SessionContext, now applies DeviceProfile preflight for app/browser/media/message/screen/camera/UI automation | Cross-platform partial | High: central runtime path | Medium: structured `result_status` and `verified` are returned, but many tools still self-report strings | Keep routing in one preflight layer; expand DeviceProfile gates to more tools as adapters improve | P0 |
| `core/session_context.py` | Runtime-only last-5 action context, vague follow-up resolver, truthfulness helpers | Cross-platform OK | Medium: wrong recent target can misroute | Medium: records verified/uncertain/failed claims | Keep private text summarization and correction attachment; do not persist to long-term memory | P0 |
| `core/device_profile.py` | DeviceProfile schema, privacy scrubber, route helpers, permission gates | Cross-platform OK | Medium: detection can be partial | High for routing decisions; does not claim external action success | Keep schema backward-compatible; add user override for preferred browser later | P0 |
| `core/environment_discovery.py` | First-run/refresh profile builder using platform adapter | Cross-platform partial | Medium: platform detections may be incomplete | Medium: stores detected/unknown/unsupported | Add deeper permission checks only when safe and non-invasive | P0 |
| `core/platform_adapters/base.py` | Shared adapter interface and safe defaults | Cross-platform OK | Low | High: unknown/unsupported by default | Keep all new OS-specific logic behind adapter methods | P0 |
| `core/platform_adapters/macos.py` | Detects app/browser/message presence, default browser best-effort, media/automation/screen permission requirements | macOS partial | Medium: TCC permission status is mostly unknown | Medium: detects capability, not action success | Add optional non-invasive permission probes where possible; keep unknown when not detectable | P1 |
| `core/platform_adapters/windows.py` | Detects common browsers/apps from PATH/common folders/registry, media key via pyautogui | Windows partial | Medium: Windows Store apps and default browser detection can vary | Medium | Add better AppX package detection and optional pywinauto capability probe | P1 |
| `core/platform_adapters/linux.py` | Detects binaries, xdg default browser, playerctl/wmctrl/xdotool, Wayland caveat | Linux partial | Medium: desktop environment and Wayland restrictions vary | Medium | Add desktop-file scan and portal-aware screen capture notes | P1 |
| `actions/open_app.py` | Launches apps via Windows shell/start menu, macOS `open -a`, Linux binaries/xdg/gtk | Cross-platform partial | Medium: may report opened after launch attempt without verifying foreground app | Low-medium | Route known app aliases through DeviceProfile first; later replace internals with adapter `launch_app()` | P1 |
| `actions/browser_control.py` | Playwright browser sessions, real/fallback profiles, default browser fallback historically guessed Chrome | Cross-platform partial | Medium: real profile paths and browser availability vary | Medium: browser page result strings verify URL/action partly | Keep DeviceProfile browser routing before session creation; remove internal Chrome default fallback later | P0 |
| `actions/media_control.py` | Safe pause/play-pause; macOS browser media verification when possible; non-macOS adapter media key/playerctl | Cross-platform partial | Medium: media key is often unverified and can toggle play if already paused | Medium on browser JS verification, low for system media key | Add platform-specific verification where safe; never close/kill apps without confirmation | P0 |
| `actions/send_message.py` | Desktop automation drafts/sends through common messaging apps; requires confirmation for actual send but cannot verify recipient/delivery | Cross-platform partial | High: can affect real messages | Low: recipient/chat/delivery not verified | Keep DeviceProfile app check and confirmation gate; add per-app verification before any "sent" claim | P0 |
| `actions/screen_processor.py` | Captures screen via `mss`, camera via OpenCV, analyzes through Gemini | Cross-platform partial | High: screen/camera permissions and privacy | Medium: capture failure raises; content is not stored by DeviceProfile | Keep DeviceProfile permission gate; do not save screenshots/audio in profile | P0 |
| `actions/computer_settings.py` | OS hotkeys/settings through pyautogui, osascript, PowerShell, nmcli/pactl/wmctrl/systemctl | Cross-platform partial | High: power/network/window actions can be disruptive | Low-medium: many functions assume success | Expand DeviceProfile preflight per action; keep restart/shutdown confirmation | P1 |
| `actions/computer_control.py` | Direct pyautogui control, screenshots, focus/window interactions | Permission-dependent | High: real UI automation can click/type wrong target | Low-medium | Gate with DeviceProfile UI automation and screen capture; add active-window verification | P1 |
| `actions/desktop.py` | Desktop wallpaper/organize/list/task helpers with OS-specific behavior | Cross-platform partial | Medium: file-moving/desktop actions can affect user files | Medium for file actions, low for UI actions | Add DeviceProfile launch/UI gates and dry-run option for destructive organize/clean | P2 |
| `actions/reminder.py` | Windows Task Scheduler/msg, macOS launchd/osascript, Linux systemd/at | Cross-platform partial | Medium: scheduler availability varies | Medium: command return codes are checked in some paths | Store scheduler capability in DeviceProfile later | P2 |
| `actions/youtube_video.py` | Opens YouTube/search/summary paths with browser/system opener and pyautogui | Cross-platform partial | Medium: browser choice and media state can be guessed | Low-medium | Route browser choice through DeviceProfile and record media target in SessionContext | P1 |
| `actions/game_updater.py` | Steam/Epic install/update/schedule/status; broad OS-specific code | Platform-specific partial | High: real installs, shutdown scheduling, game launchers | Medium for process/status checks, low for UI dialogs | Add DeviceProfile app detection for Steam/Epic and stronger confirmation around shutdown/install | P2 |
| `actions/web_search.py` | Web/news/research/price via network APIs/search | Cross-platform OK | Low-medium: network rate limits | Medium: returns search result/error text | No DeviceProfile integration required except network status later | P3 |
| `actions/file_processor.py` | Local file processing by type | Cross-platform OK | Medium: reads/writes local files | Medium: output paths/results returned | Keep path privacy; do not put private file contents in DeviceProfile or SessionContext | P2 |
| `actions/file_controller.py` | File/folder create/delete/move/copy/read/write/find | Cross-platform OK with filesystem caveats | High: destructive file operations | Medium: filesystem result can be checked | Add confirmation for destructive broad actions; keep DeviceProfile project path awareness | P1 |
| `actions/code_helper.py` | Writes/runs/builds code files | Cross-platform partial | Medium-high: executes local code | Medium: subprocess result available | Keep sandboxing and explicit user intent; no DeviceProfile need beyond shell/platform info | P2 |
| `actions/dev_agent.py` | Multi-step project generation/install/run | Cross-platform partial | High: can install deps/run commands | Medium | Require explicit install rationale; use DeviceProfile shell/platform info for commands | P2 |
| `actions/proactive.py` | Silence/proactive prompts | Cross-platform OK | Low | N/A | No DeviceProfile integration needed | P3 |
| `actions/system_monitor.py` | CPU/RAM/temp/process metrics | Cross-platform partial | Low | Medium: psutil/sensors may be unavailable | Add DeviceProfile metric capability later | P3 |
| `core/i18n.py` | English/Russian UI dictionary and settings | Cross-platform OK | Low | High | Keep new visible fixed UI text bilingual | P1 |

## Current P0 Integration Status

- `main.py` loads/creates DeviceProfile on startup.
- `main.py` injects DeviceProfile summary into the model prompt.
- `main.py` exposes `device_profile` summary/query/refresh.
- Browser, app, media, messaging, screen/camera, and UI automation tool calls have DeviceProfile preflight.
- Local `config/device_profile.json` is gitignored.

## Remaining Risks

- Device detection is intentionally best-effort. Unknown must remain unknown.
- macOS TCC permission state is not reliably read without invasive probes, so the profile records unknown/permission-required where appropriate.
- Windows Store apps, Linux desktop files, and browser default handlers may need deeper platform-specific detection later.
- Many action modules still contain direct OS logic internally. v0.4 adds a reusable routing/preflight layer first; later work can migrate internals to adapters.
