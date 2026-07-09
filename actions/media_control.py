import platform
import subprocess
import time
from typing import Any

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False


_OS = platform.system()

_BROWSER_APP_NAMES = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "safari": "Safari",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "brave": "Brave Browser",
    "brave browser": "Brave Browser",
    "opera": "Opera",
}

_JS_PAUSE_AND_VERIFY = """
(() => {
  const media = Array.from(document.querySelectorAll('video,audio'));
  media.forEach((item) => { try { item.pause(); } catch (_) {} });
  return JSON.stringify({
    count: media.length,
    anyPlaying: media.some((item) => !item.paused && !item.ended)
  });
})()
""".strip()


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_target_app(value: Any) -> str:
    text = _clean(value)
    lowered = text.lower()
    if not lowered:
        return ""
    if "chatgpt atlas" in lowered or "gpt atlas" in lowered:
        return "ChatGPT Atlas"
    return _BROWSER_APP_NAMES.get(lowered, text)


def _run_osascript(script: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, output
    except Exception as e:
        return False, str(e)


def _send_macos_media_pause() -> tuple[bool, str]:
    script = 'tell application "System Events" to key code 16'
    ok, detail = _run_osascript(script, timeout=2.0)
    if ok:
        return True, "System Events media play/pause key sent."

    if _PYAUTOGUI:
        try:
            pyautogui.press("playpause")
            return True, "pyautogui playpause key sent."
        except Exception as e:
            detail = f"{detail}; pyautogui playpause failed: {e}"

    return False, detail or "macOS media pause command failed."


def _pause_chromium_browser(app_name: str) -> tuple[bool | None, str]:
    escaped_js = _JS_PAUSE_AND_VERIFY.replace('"', '\\"')
    script = f'''
tell application "{app_name}"
    if (count of windows) = 0 then return "no_window"
    tell active tab of front window
        execute javascript "{escaped_js}"
    end tell
end tell
'''.strip()
    ok, detail = _run_osascript(script, timeout=3.0)
    if not ok:
        return None, detail
    return _interpret_media_verification(detail)


def _pause_safari(app_name: str) -> tuple[bool | None, str]:
    escaped_js = _JS_PAUSE_AND_VERIFY.replace('"', '\\"')
    script = f'''
tell application "{app_name}"
    if (count of windows) = 0 then return "no_window"
    do JavaScript "{escaped_js}" in current tab of front window
end tell
'''.strip()
    ok, detail = _run_osascript(script, timeout=3.0)
    if not ok:
        return None, detail
    return _interpret_media_verification(detail)


def _interpret_media_verification(detail: str) -> tuple[bool | None, str]:
    cleaned = _clean(detail)
    if not cleaned:
        return None, "No browser media verification output."
    if '"count":0' in cleaned or "no_window" in cleaned:
        return None, cleaned
    if '"anyPlaying":false' in cleaned:
        return True, cleaned
    if '"anyPlaying":true' in cleaned:
        return False, cleaned
    return None, cleaned


def _pause_browser_media(target_app: str) -> tuple[bool | None, str]:
    app_name = _normalize_target_app(target_app)
    if not app_name:
        return None, "No target browser/app provided."
    if app_name in {"Google Chrome", "Microsoft Edge", "Brave Browser", "Opera"}:
        return _pause_chromium_browser(app_name)
    if app_name == "Safari":
        return _pause_safari(app_name)
    return None, f"No safe browser media verifier is available for {app_name}."


def media_control(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = _clean(params.get("action") or "pause").lower().replace("-", "_")
    target_app = _normalize_target_app(params.get("target_app"))
    target_context = _clean(params.get("target_context"))
    fallback_level = _clean(params.get("fallback_level")).lower()

    if action in {"stop", "media_stop"}:
        action = "pause"
    if action not in {"pause", "media_pause", "play_pause"}:
        return f"Unsupported media action: {action}"

    target_label = target_app or target_context or "system media"
    if player:
        player.write_log(f"[Media] pause requested: {target_label}")

    if _OS != "Darwin":
        if _PYAUTOGUI:
            try:
                pyautogui.press("playpause")
                return (
                    "Media pause/play-pause command sent, but playback status was not verified."
                )
            except Exception as e:
                return f"Could not send media pause command: {e}"
        return "Media pause is unavailable because pyautogui is not installed."

    native_ok, native_detail = _send_macos_media_pause()
    time.sleep(0.2)

    verified, verify_detail = _pause_browser_media(target_app)
    if verified is True:
        return f"Media paused and verified for {target_label}."
    if verified is False:
        return (
            f"Mac media pause command was sent for {target_label}, "
            "but browser verification still detected active playback."
        )

    if native_ok:
        suffix = ""
        if fallback_level == "stronger" and not target_app:
            suffix = " Tell me which app/browser is playing before I close or kill anything."
        return (
            f"Mac media pause command sent for {target_label}, "
            "but playback status was not verified."
            f"{suffix}"
        )

    return f"Could not send macOS media pause command for {target_label}: {native_detail}"
