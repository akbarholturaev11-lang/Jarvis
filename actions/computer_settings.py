#computer_settings.py
import json
import re
import sys
import time
import subprocess
import platform
from pathlib import Path

from core.credential_service import require_gemini_api_key
from core.app_paths import resolve_app_paths

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

if _OS == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}


def _get_base_dir() -> Path:
    return resolve_app_paths().resource_root

def _get_api_key() -> str:
    path = _get_base_dir() / "config" / "api_keys.json"
    return require_gemini_api_key(legacy_path=path)

def _get_macos_wifi_interface() -> str:
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i, min(i + 4, len(lines))):
                    if lines[j].startswith("Device:"):
                        return lines[j].split(":", 1)[1].strip()
    except Exception:
        pass
    return "en0" 

def volume_up():
    if _OS == "Windows":
        for _ in range(5): pyautogui.press("volumeup")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) + 10)"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
            capture_output=True)

def volume_down():
    if _OS == "Windows":
        for _ in range(5): pyautogui.press("volumedown")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) - 10)"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"],
            capture_output=True)

def volume_mute():
    if _OS == "Windows":
        pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", "set volume with output muted"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
            capture_output=True)

def volume_set(value: int):
    value = max(0, min(100, int(value)))
    if _OS == "Windows":
        try:
            import math
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices   = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol       = cast(interface, POINTER(IAudioEndpointVolume))
            vol_db    = -65.25 if value == 0 else max(-65.25, 20 * math.log10(value / 100))
            vol.SetMasterVolumeLevel(vol_db, None)
            return
        except Exception as e:
            print(f"[Settings] pycaw failed, using keypress fallback: {e}")
            pyautogui.press("volumemute")
            pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {value}"],
            capture_output=True)
        return
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
            capture_output=True)
        return

def brightness_up():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 144'],
            capture_output=True)
    elif _OS == "Linux":
        if subprocess.run(["which", "brightnessctl"],
                capture_output=True).returncode == 0:
            subprocess.run(["brightnessctl", "set", "+10%"], capture_output=True)
        else:
            subprocess.run(
                'xrandr --output $(xrandr | grep " connected" | head -1 | cut -d " " -f1)'
                ' --brightness $(python3 -c "import subprocess; '
                'b=float(subprocess.check_output([\"xrandr\",\"--verbose\"]).decode()'
                '.split(\"Brightness:\")[1].split()[0]); print(min(1.0,b+0.1))")',
                shell=True, capture_output=True
            )
    else:
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                 ".WmiSetBrightness(1, [math]::Min(100, "
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness + 10))"],
                capture_output=True, timeout=5, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] Brightness up failed on Windows: {e}")

def brightness_down():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 145'],
            capture_output=True)
    elif _OS == "Linux":
        if subprocess.run(["which", "brightnessctl"],
                capture_output=True).returncode == 0:
            subprocess.run(["brightnessctl", "set", "10%-"], capture_output=True)
        else:
            subprocess.run(
                'xrandr --output $(xrandr | grep " connected" | head -1 | cut -d " " -f1)'
                ' --brightness $(python3 -c "import subprocess; '
                'b=float(subprocess.check_output([\"xrandr\",\"--verbose\"]).decode()'
                '.split(\"Brightness:\")[1].split()[0]); print(max(0.1,b-0.1))")',
                shell=True, capture_output=True
            )
    else:
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                 ".WmiSetBrightness(1, [math]::Max(0, "
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness - 10))"],
                capture_output=True, timeout=5, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] Brightness down failed on Windows: {e}")

def close_app():
    if _OS == "Darwin": pyautogui.hotkey("command", "q")
    else:               pyautogui.hotkey("alt", "f4")

def close_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "w")
    else:               pyautogui.hotkey("ctrl", "w")

def full_screen():
    if _OS == "Darwin": pyautogui.hotkey("ctrl", "command", "f")
    else:               pyautogui.press("f11")

def minimize_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "m")
    else:               pyautogui.hotkey("win", "down")

def maximize_window():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to keystroke "f" '
            'using {control down, command down}'],
            capture_output=True)
    elif _OS == "Windows":
        pyautogui.hotkey("win", "up")
    else:
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-b", "add,maximized_vert,maximized_horz"],
                capture_output=True)
        except Exception:
            pyautogui.hotkey("super", "up")

def snap_left():
    if _OS == "Windows":
        pyautogui.hotkey("win", "left")
    elif _OS == "Linux":
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,0,0,960,1080"],
                capture_output=True)
        except Exception:
            pass

def snap_right():
    if _OS == "Windows":
        pyautogui.hotkey("win", "right")
    elif _OS == "Linux":
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,960,0,960,1080"],
                capture_output=True)
        except Exception:
            pass

def switch_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "tab")
    else:               pyautogui.hotkey("alt", "tab")

def show_desktop():
    if _OS == "Darwin":   pyautogui.hotkey("fn", "f11")
    elif _OS == "Windows": pyautogui.hotkey("win", "d")
    else:                  pyautogui.hotkey("super", "d")

def open_task_manager():
    if _OS == "Windows":
        pyautogui.hotkey("ctrl", "shift", "esc")
    elif _OS == "Darwin":
        subprocess.Popen(["open", "-a", "Activity Monitor"])
    else:
        for cmd in [["gnome-system-monitor"], ["xfce4-taskmanager"], ["htop"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                break


def focus_search():
    if _OS == "Darwin": pyautogui.hotkey("command", "l")
    else:               pyautogui.hotkey("ctrl", "l")

def pause_video():      pyautogui.press("space")

def refresh_page():
    if _OS == "Darwin": pyautogui.hotkey("command", "r")
    else:               pyautogui.press("f5")

def close_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "w")
    else:               pyautogui.hotkey("ctrl", "w")

def new_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "t")
    else:               pyautogui.hotkey("ctrl", "t")

def next_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "bracketright")
    else:               pyautogui.hotkey("ctrl", "tab")

def prev_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "bracketleft")
    else:               pyautogui.hotkey("ctrl", "shift", "tab")

def go_back():
    if _OS == "Darwin": pyautogui.hotkey("command", "left")
    else:               pyautogui.hotkey("alt", "left")

def go_forward():
    if _OS == "Darwin": pyautogui.hotkey("command", "right")
    else:               pyautogui.hotkey("alt", "right")

def zoom_in():
    if _OS == "Darwin": pyautogui.hotkey("command", "equal")
    else:               pyautogui.hotkey("ctrl", "equal")

def zoom_out():
    if _OS == "Darwin": pyautogui.hotkey("command", "minus")
    else:               pyautogui.hotkey("ctrl", "minus")

def zoom_reset():
    if _OS == "Darwin": pyautogui.hotkey("command", "0")
    else:               pyautogui.hotkey("ctrl", "0")

def find_on_page():
    if _OS == "Darwin": pyautogui.hotkey("command", "f")
    else:               pyautogui.hotkey("ctrl", "f")

def reload_page_n(n: int):
    for _ in range(max(1, n)):
        refresh_page()
        time.sleep(0.8)


def scroll_up(amount: int = 500):    pyautogui.scroll(amount)
def scroll_down(amount: int = 500):  pyautogui.scroll(-amount)

def scroll_top():
    if _OS == "Darwin": pyautogui.hotkey("command", "up")
    else:               pyautogui.hotkey("ctrl", "home")

def scroll_bottom():
    if _OS == "Darwin": pyautogui.hotkey("command", "down")
    else:               pyautogui.hotkey("ctrl", "end")

def page_up():   pyautogui.press("pageup")
def page_down(): pyautogui.press("pagedown")


def copy():
    if _OS == "Darwin": pyautogui.hotkey("command", "c")
    else:               pyautogui.hotkey("ctrl", "c")

def paste():
    if _OS == "Darwin": pyautogui.hotkey("command", "v")
    else:               pyautogui.hotkey("ctrl", "v")

def cut():
    if _OS == "Darwin": pyautogui.hotkey("command", "x")
    else:               pyautogui.hotkey("ctrl", "x")

def undo():
    if _OS == "Darwin": pyautogui.hotkey("command", "z")
    else:               pyautogui.hotkey("ctrl", "z")

def redo():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "z")
    else:               pyautogui.hotkey("ctrl", "y")

def select_all():
    if _OS == "Darwin": pyautogui.hotkey("command", "a")
    else:               pyautogui.hotkey("ctrl", "a")

def save_file():
    if _OS == "Darwin": pyautogui.hotkey("command", "s")
    else:               pyautogui.hotkey("ctrl", "s")

def press_enter():   pyautogui.press("enter")
def press_escape():  pyautogui.press("escape")
def press_key(key: str): pyautogui.press(key)

def type_text(text: str, press_enter_after: bool = False):
    if not text:
        return
    if _PYPERCLIP:
        pyperclip.copy(str(text))
        time.sleep(0.15)
        paste()
    else:
        pyautogui.write(str(text), interval=0.03)
    if press_enter_after:
        time.sleep(0.1)
        pyautogui.press("enter")

def take_screenshot():
    if _OS == "Windows":
        pyautogui.hotkey("win", "shift", "s")
    elif _OS == "Darwin":
        pyautogui.hotkey("command", "shift", "3")
    else:
        for cmd in [["scrot"], ["gnome-screenshot"], ["import", "-window", "root", "screenshot.png"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return
        pyautogui.hotkey("ctrl", "print_screen")

def lock_screen():
    if _OS == "Windows":
        pyautogui.hotkey("win", "l")
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
    else:
        for cmd in [
            ["gnome-screensaver-command", "-l"],
            ["xdg-screensaver", "lock"],
            ["loginctl", "lock-session"],
        ]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.run(cmd, capture_output=True)
                return

def open_system_settings():
    if _OS == "Windows":
        pyautogui.hotkey("win", "i")
    elif _OS == "Darwin":
        subprocess.Popen(["open", "-a", "System Preferences"])
    else:
        for cmd in [["gnome-control-center"], ["xfce4-settings-manager"], ["kcmshell5"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return

def open_file_explorer():
    if _OS == "Windows":
        pyautogui.hotkey("win", "e")
    elif _OS == "Darwin":
        subprocess.Popen(["open", str(Path.home())])
    else:
        for cmd in [["nautilus"], ["thunar"], ["dolphin"], ["nemo"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return
        subprocess.Popen(["xdg-open", str(Path.home())])

def sleep_display():
    if _OS == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        except Exception as e:
            print(f"[Settings] sleep_display failed: {e}")
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
    else:
        subprocess.run(["xset", "dpms", "force", "off"], capture_output=True)

def open_run():
    if _OS == "Windows":
        pyautogui.hotkey("win", "r")

def dark_mode():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to tell appearance preferences '
            'to set dark mode to not dark mode'],
            capture_output=True)
    elif _OS == "Windows":
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
            current, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, 1 - current)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, 1 - current)
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[Settings] dark_mode registry failed: {e}")
    else:
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True
            )
            current = result.stdout.strip()
            new_scheme = "'default'" if "dark" in current else "'prefer-dark'"
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", new_scheme],
                capture_output=True
            )
        except Exception as e:
            print(f"[Settings] dark_mode Linux failed: {e}")

def toggle_wifi():
    if _OS == "Darwin":
        iface = _get_macos_wifi_interface()
        result = subprocess.run(
            ["networksetup", "-getairportpower", iface],
            capture_output=True, text=True
        )
        state = "off" if "On" in result.stdout else "on"
        subprocess.run(["networksetup", "-setairportpower", iface, state],
            capture_output=True)
    elif _OS == "Windows":
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "$adapter = Get-NetAdapter | Where-Object {$_.PhysicalMediaType -eq 'Native 802.11'};"
                 "if ($adapter.Status -eq 'Up') { Disable-NetAdapter -Name $adapter.Name -Confirm:$false }"
                 "else { Enable-NetAdapter -Name $adapter.Name -Confirm:$false }"],
                capture_output=True, timeout=10, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] toggle_wifi Windows failed: {e}")
    else:
        try:
            result = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True)
            state  = "off" if "enabled" in result.stdout else "on"
            subprocess.run(["nmcli", "radio", "wifi", state], capture_output=True)
        except Exception as e:
            print(f"[Settings] toggle_wifi Linux failed: {e}")

def restart_computer():
    if _OS == "Windows":
        subprocess.run(["shutdown", "/r", "/t", "10"], capture_output=True, **_WIN_HIDE)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to restart'],
            capture_output=True)
    else:
        subprocess.run(["systemctl", "reboot"], capture_output=True)

def shutdown_computer():
    if _OS == "Windows":
        subprocess.run(["shutdown", "/s", "/t", "10"], capture_output=True)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to shut down'],
            capture_output=True)
    else:
        subprocess.run(["systemctl", "poweroff"], capture_output=True)

ACTION_MAP: dict[str, callable] = {
    "volume_up":           volume_up,
    "volume_down":         volume_down,
    "mute":                volume_mute,
    "unmute":              volume_mute,
    "toggle_mute":         volume_mute,
    "brightness_up":       brightness_up,
    "brightness_down":     brightness_down,
    "sleep_display":       sleep_display,
    "screen_off":          sleep_display,
    "pause_video":         pause_video,
    "play_pause":          pause_video,
    "close_app":           close_app,
    "close_window":        close_window,
    "full_screen":         full_screen,
    "fullscreen":          full_screen,
    "minimize":            minimize_window,
    "maximize":            maximize_window,
    "snap_left":           snap_left,
    "snap_right":          snap_right,
    "switch_window":       switch_window,
    "show_desktop":        show_desktop,
    "task_manager":        open_task_manager,
    "focus_search":        focus_search,
    "refresh_page":        refresh_page,
    "reload":              refresh_page,
    "close_tab":           close_tab,
    "new_tab":             new_tab,
    "next_tab":            next_tab,
    "prev_tab":            prev_tab,
    "go_back":             go_back,
    "go_forward":          go_forward,
    "zoom_in":             zoom_in,
    "zoom_out":            zoom_out,
    "zoom_reset":          zoom_reset,
    "find_on_page":        find_on_page,
    "scroll_up":           scroll_up,
    "scroll_down":         scroll_down,
    "scroll_top":          scroll_top,
    "scroll_bottom":       scroll_bottom,
    "page_up":             page_up,
    "page_down":           page_down,
    "copy":                copy,
    "paste":               paste,
    "cut":                 cut,
    "undo":                undo,
    "redo":                redo,
    "select_all":          select_all,
    "save":                save_file,
    "enter":               press_enter,
    "escape":              press_escape,
    "screenshot":          take_screenshot,
    "lock_screen":         lock_screen,
    "open_settings":       open_system_settings,
    "file_explorer":       open_file_explorer,
    "open_run":            open_run,
    "dark_mode":           dark_mode,
    "toggle_wifi":         toggle_wifi,
    "restart":             restart_computer,
    "shutdown":            shutdown_computer,
}

_DANGEROUS_ACTIONS = {"restart", "shutdown"}



def _detect_action(description: str) -> dict:

    from google import genai as _genai
    _client = _genai.Client(api_key=_get_api_key())

    available = ", ".join(sorted(ACTION_MAP.keys())) + \
                ", volume_set, type_text, press_key, reload_n"

    prompt = f"""You are an intent detector for a computer control assistant.

The user issued a command (possibly in any language): "{description}"

Available actions: {available}

Return ONLY a valid JSON object:
{{"action": "action_name", "value": null_or_value}}

Rules:
- Pick the single best matching action from the available list.
- For volume_set: value is an integer 0-100.
- For type_text: value is the exact text to type.
- For press_key: value is the key name (e.g. "f5", "tab", "enter").
- For reload_n: value is an integer (number of times to reload).
- If no clear match, pick the closest action.
- Return ONLY the JSON, no explanation, no markdown."""

    try:
        resp = _client.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
        text = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Settings] Intent detection failed: {e}")
        return {"action": description.lower().replace(" ", "_"), "value": None}


# ── Honest execution helpers ────────────────────────────────────────────────
# These verify real OS outcomes instead of assuming success, and surface the
# macOS Accessibility / Automation permission requirement clearly. The three
# possible outcomes are: verified success, "requested, unverified", and failed.

_ACCESSIBILITY_HINT = (
    "macOS Accessibility permission is required. Enable it for Terminal "
    "(or the JARVIS app) in System Settings > Privacy & Security > Accessibility, "
    "then fully quit and restart the app."
)
_AUTOMATION_HINT = (
    "macOS Automation permission is required. Allow Terminal (or the JARVIS app) "
    "to control the target app in System Settings > Privacy & Security > Automation, "
    "then restart the app."
)

# macOS ACTION_MAP entries that drive the screen through pyautogui or a
# "System Events" keystroke, so they need Accessibility permission and cannot
# be verified after the fact.
_ACCESSIBILITY_ACTIONS = {
    "pause_video", "play_pause",
    "close_app", "close_window",
    "full_screen", "fullscreen",
    "minimize", "maximize",
    "switch_window", "show_desktop",
    "focus_search", "refresh_page", "reload",
    "close_tab", "new_tab", "next_tab", "prev_tab",
    "go_back", "go_forward",
    "zoom_in", "zoom_out", "zoom_reset", "find_on_page",
    "scroll_top", "scroll_bottom", "page_up", "page_down",
    "copy", "paste", "cut", "undo", "redo", "select_all", "save",
    "enter", "escape", "screenshot",
}

_accessibility_cache: dict = {"checked": False, "value": None}


def _classify_permission_error(returncode, stderr: str):
    """Return 'accessibility' | 'automation' | None for an osascript failure."""
    if returncode == 0:
        return None
    s = (stderr or "").lower()
    if "-25211" in s or "assistive access" in s or "accessibility" in s:
        return "accessibility"
    if (
        "-1743" in s
        or "-1744" in s
        or "not authori" in s          # "not authorised/authorized to send Apple events"
        or "not allowed to send apple events" in s
    ):
        return "automation"
    return None


def _osa(script: str, timeout: float = 5.0) -> dict:
    """Run an osascript command and report the real outcome."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
        stderr = (proc.stderr or "").strip()
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "").strip(),
            "stderr": stderr,
            "timed_out": False,
            "permission": _classify_permission_error(proc.returncode, stderr),
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "timed out",
                "timed_out": True, "permission": None}
    except Exception as e:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(e),
                "timed_out": False, "permission": None}


def _accessibility_available():
    """True/False/None whether this process may drive UI on macOS (cached).

    On non-macOS, pyautogui does not depend on the macOS Accessibility TCC
    grant, so the gate is a no-op (returns True).
    """
    if _OS != "Darwin":
        return True
    if _accessibility_cache["checked"]:
        return _accessibility_cache["value"]
    res = _osa('tell application "System Events" to get name of first process', timeout=3.0)
    if res["ok"]:
        value = True
    elif res["permission"] == "accessibility":
        value = False
    else:
        value = None  # unknown: automation prompt pending, timeout, or other error
    _accessibility_cache["checked"] = True
    _accessibility_cache["value"] = value
    return value


def _read_output_volume():
    if _OS != "Darwin":
        return None
    res = _osa("output volume of (get volume settings)", timeout=3.0)
    if res["ok"] and res["stdout"].lstrip("-").isdigit():
        return int(res["stdout"])
    return None


def _read_output_muted():
    if _OS != "Darwin":
        return None
    res = _osa("output muted of (get volume settings)", timeout=3.0)
    if res["ok"]:
        v = res["stdout"].strip().lower()
        if v in ("true", "false"):
            return v == "true"
    return None


def _accessibility_gate(action: str):
    """Return a permission-failure message if Accessibility is denied, else None."""
    if _OS == "Darwin" and _accessibility_available() is False:
        return f"Could not run '{action}': {_ACCESSIBILITY_HINT}"
    return None


def _verified_or_unverified(success_msg: str, action: str) -> str:
    """Downgrade an unverifiable pyautogui success to an honest unverified status
    when Accessibility cannot be confirmed granted."""
    if _OS == "Darwin" and _accessibility_available() is not True:
        return (
            f"{action} requested, unverified: the key event was sent but the "
            "on-screen effect could not be confirmed."
        )
    return success_msg


def _handle_volume(action: str, value) -> str:
    """Change volume and verify the real output level changed (macOS)."""
    if _OS != "Darwin":
        try:
            if action == "volume_set":
                target = max(0, min(100, int(value if value is not None else 50)))
                volume_set(target)
                return f"Volume set to {target}%."
            ACTION_MAP[action]()
            return f"Done: {action}."
        except Exception as e:
            return f"Could not change volume ({action}): {e}"

    if action in ("mute", "unmute", "toggle_mute"):
        before = _read_output_muted()
        want = True if action == "mute" else False if action == "unmute" else (not before if before is not None else True)
        script = "set volume with output muted" if want else "set volume without output muted"
        res = _osa(script, timeout=3.0)
        if not res["ok"]:
            reason = res["stderr"] or "osascript error"
            return f"Could not change mute state: {reason}."
        after = _read_output_muted()
        if after is None:
            return "Mute command sent but could not confirm the state (requested, unverified)."
        if after == want:
            return f"Done: {'mute' if want else 'unmute'} (verified: muted={after})."
        return "Mute command sent but the state did not change (requested, unverified)."

    before = _read_output_volume()
    if action == "volume_set":
        target = max(0, min(100, int(value if value is not None else 50)))
        res = _osa(f"set volume output volume {target}", timeout=3.0)
        if not res["ok"]:
            return f"Could not set volume: {res['stderr'] or 'osascript error'}."
        after = _read_output_volume()
        if after is None:
            return "Volume command sent but could not confirm the level (requested, unverified)."
        if abs(after - target) <= 3:
            return f"Volume set to {after}% (verified)."
        return (
            f"Volume command sent but the level is {after}% instead of {target}% "
            "(requested, unverified)."
        )

    up = action == "volume_up"
    delta = "+ 10" if up else "- 10"
    res = _osa(
        f"set volume output volume ((output volume of (get volume settings)) {delta})",
        timeout=3.0,
    )
    if not res["ok"]:
        return f"Could not change volume: {res['stderr'] or 'osascript error'}."
    after = _read_output_volume()
    if before is None or after is None:
        return "Volume command sent but could not confirm the change (requested, unverified)."
    if after != before:
        return f"Volume set to {after}% (verified: {before}% -> {after}%)."
    if (up and before >= 100) or (not up and before <= 0):
        return f"Volume set to {after}% (verified: already at the limit)."
    return "Volume command sent but the level did not change (requested, unverified)."


def _handle_brightness(action: str) -> str:
    """Change brightness; brightness cannot be reliably read back on macOS."""
    if _OS != "Darwin":
        try:
            ACTION_MAP[action]()
            return f"Done: {action}."
        except Exception as e:
            return f"Could not change brightness ({action}): {e}"

    key = 144 if action == "brightness_up" else 145
    res = _osa(f'tell application "System Events" to key code {key}', timeout=3.0)
    if res["ok"]:
        return (
            "Brightness change requested, unverified: the key was sent but macOS "
            "does not expose a reliable brightness value to confirm the change."
        )
    if res["permission"] == "accessibility" or _accessibility_available() is False:
        return f"Could not change brightness: {_ACCESSIBILITY_HINT}"
    if res["timed_out"]:
        return "Could not change brightness: the command timed out."
    return f"Could not change brightness: {res['stderr'] or 'osascript error'}."


def computer_settings(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if not _PYAUTOGUI:
        return "pyautogui is not installed. Run: pip install pyautogui"

    params      = parameters or {}
    raw_action  = params.get("action", "").strip()
    description = params.get("description", "").strip()
    value       = params.get("value", None)

    if not raw_action and description:
        detected   = _detect_action(description)
        raw_action = detected.get("action", "")
        if value is None:
            value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")

    if not action:
        return "No action could be determined."

    print(f"[Settings] Action: {action}  Value: {value}  OS: {_OS}")
    if player:
        player.write_log(f"[Settings] {action}")

    if action in _DANGEROUS_ACTIONS:
        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return (
                f"This will {action} the computer. "
                f"Please confirm by calling again with confirmed=yes."
            )

    if action in ("volume_up", "volume_down", "volume_set", "mute", "unmute", "toggle_mute"):
        return _handle_volume(action, value)

    if action in ("brightness_up", "brightness_down"):
        return _handle_brightness(action)

    if action in ("type_text", "write_on_screen", "type", "write"):
        text = str(value or params.get("text", "")).strip()
        if not text:
            return "No text provided to type."
        gate = _accessibility_gate("type_text")
        if gate:
            return gate
        enter_after = str(params.get("press_enter", "false")).lower() in ("true", "1", "yes")
        type_text(text, press_enter_after=enter_after)
        return _verified_or_unverified(f"Typed: {text[:80]}", "type_text")

    if action == "press_key":
        key = str(value or params.get("key", "")).strip()
        if not key:
            return "No key specified."
        gate = _accessibility_gate("press_key")
        if gate:
            return gate
        press_key(key)
        return _verified_or_unverified(f"Pressed: {key}", "press_key")

    if action in ("reload_n", "refresh_n", "reload_page_n"):
        gate = _accessibility_gate("reload_n")
        if gate:
            return gate
        try:
            reload_page_n(int(value or 1))
            return _verified_or_unverified(f"Reloaded {value or 1} time(s).", "reload_n")
        except Exception as e:
            return f"Reload failed: {e}"

    if action == "scroll_up":
        gate = _accessibility_gate("scroll_up")
        if gate:
            return gate
        scroll_up(int(value or 500))
        return _verified_or_unverified("Scrolled up.", "scroll_up")

    if action == "scroll_down":
        gate = _accessibility_gate("scroll_down")
        if gate:
            return gate
        scroll_down(int(value or 500))
        return _verified_or_unverified("Scrolled down.", "scroll_down")

    func = ACTION_MAP.get(action)
    if not func:
        return f"Unknown action: '{raw_action}'."

    requires_accessibility = action in _ACCESSIBILITY_ACTIONS
    if requires_accessibility:
        gate = _accessibility_gate(action)
        if gate:
            return gate

    try:
        func()
    except Exception as e:
        print(f"[Settings] Action failed ({action}): {e}")
        return f"Action failed ({action}): {e}"

    if requires_accessibility:
        return _verified_or_unverified(f"Done: {action}.", action)
    return f"Done: {action}."
