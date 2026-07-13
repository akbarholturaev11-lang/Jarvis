from __future__ import annotations

import json
import platform
import re
import subprocess
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


MAX_ACTIONS = 5
SUMMARY_LIMIT = 160
PARAM_LIMIT = 220

# ── Universal action categories ────────────────────────────────────────────
# Platform-neutral semantic buckets. Execution stays in the platform adapters;
# SessionContext only stores the semantic target + metadata per category.
ACTION_CATEGORY_APP = "app_lifecycle"
ACTION_CATEGORY_BROWSER = "browser"
ACTION_CATEGORY_MEDIA = "media"
ACTION_CATEGORY_FILE = "file"
ACTION_CATEGORY_MESSAGING = "messaging"
ACTION_CATEGORY_REMINDER = "reminder"
ACTION_CATEGORY_SEARCH = "search"
ACTION_CATEGORY_SCREEN = "screen"
ACTION_CATEGORY_CAMERA = "camera"
ACTION_CATEGORY_SETTINGS = "settings"
ACTION_CATEGORY_BRIEFING = "briefing"
ACTION_CATEGORY_SYSTEM = "system"
ACTION_CATEGORY_DEV = "dev"
ACTION_CATEGORY_OTHER = "other"

# Tools that are safe to re-run verbatim on a "yana qil" / "do it again" repeat.
SAFE_REPEATABLE_TOOLS = {
    "web_search",
    "open_app",
    "personal_briefing",
    "system_status",
    "device_profile",
    "weather_report",
    "screen_process",
}
# Tools whose repeat has a side effect and therefore needs confirmation.
DANGEROUS_REPEAT_TOOLS = {
    "send_message",
    "reminder",
    "shutdown_jarvis",
    "close_app",
}
# file_controller sub-actions that mutate the filesystem and must not auto-repeat.
DANGEROUS_FILE_ACTIONS = {
    "delete",
    "move",
    "write",
    "rename",
    "create_file",
    "create_folder",
    "copy",
    "organize_desktop",
}

BROWSER_NAMES = {
    "chrome",
    "edge",
    "firefox",
    "opera",
    "operagx",
    "brave",
    "vivaldi",
    "safari",
}
BROWSER_ALIASES = {
    "google chrome": "chrome",
    "chrome": "chrome",
    "microsoft edge": "edge",
    "ms edge": "edge",
    "edge": "edge",
    "mozilla firefox": "firefox",
    "firefox": "firefox",
    "opera gx": "operagx",
    "operagx": "operagx",
    "opera": "opera",
    "brave browser": "brave",
    "brave": "brave",
    "vivaldi": "vivaldi",
    "safari": "safari",
}
MEDIA_TARGET_ALIASES = {
    "chatgpt atlas": "ChatGPT Atlas",
    "gpt atlas": "ChatGPT Atlas",
    "atlas": "ChatGPT Atlas",
    "google chrome": "chrome",
    "chrome": "chrome",
    "safari": "safari",
    "firefox": "firefox",
    "edge": "edge",
    "microsoft edge": "edge",
    "brave": "brave",
    "opera": "opera",
    "spotify": "Spotify",
    "music": "Music",
}

_SENSITIVE_KEY_PARTS = (
    "api",
    "key",
    "token",
    "secret",
    "password",
    "credential",
    "database_url",
)
_PRIVATE_TEXT_KEYS = {
    "message",
    "message_text",
    "content",
    "text",
    "value",
    "instruction",
    "code",
}

_UNCERTAIN_PATTERNS = (
    "could not confirm",
    "not verified",
    "without a detailed verification",
    "exact status is uncertain",
    "exact outcome is uncertain",
    "attempt completed",
    "may still be",
    "still processing",
    "will not call this again",
    "please confirm",
)

_FAILURE_PATTERNS = (
    "could not ",
    "failed",
    "error",
    "not found",
    "no active",
    "unknown ",
    "please specify",
    "cannot ",
    "can't ",
    "unsupported",
    "timed out",
    "unavailable",
    "not installed",
    "not provided",
    "no action could be determined",
    "no application name provided",
    "no recipient",
)

_CONFIRMATION_PATTERNS = (
    "please confirm",
    "confirmation needed",
    "tasdiqlaysizmi",
    "qaysi app/browser",
)

_SUCCESS_STARTS = (
    "opened ",
    "opened:",
    "clicked",
    "typed",
    "text typed",
    "scrolled",
    "pressed",
    "navigated",
    "page reloaded",
    "tab closed",
    "new tab opened",
    "screenshot saved",
    "camera closed",
    "reminder set",
    "volume set",
    "done:",
    "media paused and verified",
    "media playback paused and verified",
    "device profile refreshed",
    "[device profile summary]",
    "default browser from deviceprofile",
    "telegram is detected in deviceprofile",
    "telegram is not detected in deviceprofile",
    "active browser",
    "all browsers closed",
)

_VAGUE_PATTERNS = (
    r"\bo['‘’`]?chir\b",
    r"\bto['‘’`]?xtat\b",
    r"\bpause\b",
    r"\bto['‘’`]?xtatib qo['‘’`]?y\b",
    r"\bmusiqa\s+o['‘’`]?chir\b",
    r"\byop\b",
    r"\btabni yop\b",
    r"\byubor\b",
    r"\byana qil\b",
    r"\bbekor qil\b",
    r"\bshuni yop\b",
    r"\boldingi ishni davom ettir\b",
    r"\bqayerga yubording\b",
    r"\bnima qilding\b",
    r"\bstop\b",
    r"\bclose (it|this|that)?\b",
    r"\bsend (it|this|that)?\b",
    r"\bdo (it )?again\b",
    r"\bcancel (it|this|that)?\b",
    r"\bcontinue (the )?previous\b",
    r"\bwhat did you do\b",
    r"\bwhere did you send\b",
    r"\bостанови\b",
    r"\bпауза\b",
    r"\bзакрой\b",
    r"\bотправь\b",
    r"\bотмени\b",
    r"\bhali ham o['‘’`]?ynayapti\b",
    r"\bstill playing\b",
    # ── Universal follow-up vocabulary ──────────────────────────────────
    r"\bdavom ettir\b",
    r"\bdavom et\b",
    r"\bresume\b",
    r"\bcontinue\b",
    r"\bпродолжи\b",
    r"\byana qil\b",
    r"\byana bir marta\b",
    r"\bqayta qil\b",
    r"\bagain\b",
    r"\bопять\b",
    r"\bснова\b",
    r"\borqaga qaytar\b",
    r"\bavvalgisiga qayt\b",
    r"\bavvalgiga qayt\b",
    r"\bundo\b",
    r"\bверни\b",
    r"\borqaga qayt\b",
    r"\borqaga\b",
    r"\bback\b",
    r"\bназад\b",
    r"\btahrir qil\b",
    r"\btahrirla\b",
    r"\bedit\b",
    r"\bko['‘’`]?chir\b",
    r"\bboshqa joyga\b",
    r"\bmove (it|this|that)?\b",
    r"\bvaqtini o['‘’`]?zgartir\b",
    r"\bvaqtni o['‘’`]?zgartir\b",
    r"\breschedule\b",
    r"\bikkinchisini och\b",
    r"\bbirinchisini och\b",
    r"\bshuni och\b",
    r"\bboshqasini tanla\b",
    r"\bbu emas\b",
    r"\bu emas\b",
)

_CORRECTION_PATTERNS = (
    r"\byo['‘’`]?q[, ]+noto['‘’`]?g['‘’`]?ri\b",
    r"\bnoto['‘’`]?g['‘’`]?ri\b",
    r"\bboshqa joyga yubording\b",
    r"\bhali ham o['‘’`]?ynayapti\b",
    r"\bstill playing\b",
    r"\bgpt atlas['‘’`]?da\b",
    r"\bchatgpt atlas['‘’`]?da\b",
    r"\batlas['‘’`]?da\b",
    r"\bsafari emas\b",
    r"\bchrome['‘’`]?da\b",
    r"\bsafari['‘’`]?da\b",
    r"\bmen buni demadim\b",
    r"\bbu ishlamadi\b",
    r"\bishlamadi\b",
    r"\bwrong\b",
    r"\bnot that\b",
    r"\bthat did not work\b",
    r"\bdidn['’]?t work\b",
    r"\bне так\b",
    r"\bнеправильно\b",
    r"\bbu emas\b",
    r"\bu emas\b",
    r"\bboshqasini tanla\b",
    r"\bmen ochgan emas\b",
)


@dataclass
class ActionRecord:
    timestamp: str
    user_text: str
    assistant_intent: str
    tool_name: str
    tool_parameters_summary: str
    target_app: str
    target_context: str
    execution_method: str
    result_status: str
    verified: bool
    user_visible_claim: str
    user_correction: str = ""
    # ── Universal action-context fields (optional; never force-filled) ──────
    action_id: str = ""
    action_category: str = ""
    action_name: str = ""
    target_type: str = ""
    target_url: str = ""
    target_file: str = ""
    target_project: str = ""
    operation: str = ""
    reversible: bool | None = None
    available_followups: list[str] = field(default_factory=list)
    undo_action: dict[str, Any] | None = None
    started_at: str = ""
    completed_at: str = ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _squash(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _short_text(value: Any, limit: int = SUMMARY_LIMIT) -> str:
    text = _squash(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _summarize_private_text(value: Any) -> str:
    text = _squash(value)
    if not text:
        return ""
    words = len(text.split())
    return f"<private text summary: {len(text)} chars, {words} words>"


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _normalize_browser_name(value: Any) -> str:
    lowered = _squash(value).lower()
    if not lowered:
        return ""
    if lowered in BROWSER_ALIASES:
        return BROWSER_ALIASES[lowered]
    for alias, browser in BROWSER_ALIASES.items():
        if alias in lowered:
            return browser
    return ""


def _detect_target_app_from_text(value: Any) -> str:
    lowered = _squash(value).lower()
    if not lowered:
        return ""
    if "safari emas" in lowered and not any(
        app in lowered for app in ("chrome", "gpt atlas", "chatgpt atlas", "firefox", "edge")
    ):
        return ""
    for alias, app_name in MEDIA_TARGET_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return app_name
    return ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_assistant_shutdown_text(lowered_text: str) -> bool:
    return "jarvis" in lowered_text and _contains_any(
        lowered_text,
        ("stop", "close", "shutdown", "quit", "exit", "to'xtat", "to‘xtat", "yop"),
    )


def _is_still_playing_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "hali ham o'ynayapti",
            "hali ham o‘ynayapti",
            "hali ham o’ynayapti",
            "still playing",
            "keeps playing",
            "music is still",
        ),
    )


def _is_media_stop_text(lowered_text: str) -> bool:
    return _is_still_playing_text(lowered_text) or _contains_any(
        lowered_text,
        (
            "to'xtat",
            "to‘xtat",
            "to’xtat",
            "stop",
            "pause",
            "o'chir",
            "o‘chir",
            "o’chir",
            "musiqa",
            "останови",
            "пауза",
        ),
    )


def _is_close_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "yop",
            "close",
            "tabni yop",
            "shuni yop",
            "закрой",
        ),
    )


def _is_send_text(lowered_text: str) -> bool:
    return _contains_any(lowered_text, ("yubor", "send", "отправ"))


def _is_where_sent_text(lowered_text: str) -> bool:
    return _contains_any(lowered_text, ("qayerga yubording", "where did you send"))


def _is_what_done_text(lowered_text: str) -> bool:
    return _contains_any(lowered_text, ("nima qilding", "what did you do"))


def _is_resume_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "davom ettir",
            "davom et",
            "resume",
            "continue",
            "продолж",
        ),
    )


def _is_repeat_text(lowered_text: str) -> bool:
    if _contains_any(lowered_text, ("yana qil", "qayta qil", "yana bir marta")):
        return True
    return bool(re.search(r"\b(do (it )?again|again|redo)\b", lowered_text)) or _contains_any(
        lowered_text, ("опять", "снова")
    )


def _is_undo_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "orqaga qaytar",
            "avvalgisiga qayt",
            "avvalgiga qayt",
            "bekor qil",
            "undo",
            "верни",
            "отмени",
        ),
    )


def _is_back_text(lowered_text: str) -> bool:
    # "orqaga qaytar" is undo, not browser-back — so match "qayt" only on a word
    # boundary (excludes "qaytar") and require a browser-navigation flavour.
    if _is_undo_text(lowered_text):
        return False
    return bool(re.search(r"\borqaga qayt\b", lowered_text)) or _contains_any(
        lowered_text, ("back", "назад")
    ) or bool(re.search(r"\borqaga\b", lowered_text))


def _is_edit_text(lowered_text: str) -> bool:
    return _contains_any(lowered_text, ("tahrir qil", "tahrirla", "edit"))


def _is_move_text(lowered_text: str) -> bool:
    return bool(re.search(r"\bko['‘’`]?chir\b", lowered_text)) or _contains_any(
        lowered_text, ("boshqa joyga", "move it", "move this", "move that")
    )


def _is_reschedule_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "vaqtini o'zgartir",
            "vaqtini o‘zgartir",
            "vaqtini o’zgartir",
            "vaqtni o'zgartir",
            "vaqtni o‘zgartir",
            "reschedule",
        ),
    )


def _is_open_result_text(lowered_text: str) -> bool:
    return _contains_any(
        lowered_text,
        (
            "ikkinchisini och",
            "birinchisini och",
            "uchinchisini och",
            "shuni och",
            "boshqasini tanla",
            "open the second",
            "open the first",
        ),
    )


def _safe_param_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return "[redacted]"
    if key.lower() in _PRIVATE_TEXT_KEYS:
        return _summarize_private_text(value)
    if isinstance(value, str):
        return _short_text(value, 90)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_short_text(item, 50) for item in value[:5]]
    if isinstance(value, dict):
        return {
            str(k)[:40]: _safe_param_value(str(k), v)
            for k, v in list(value.items())[:8]
        }
    return _short_text(value, 90)


def summarize_tool_parameters(parameters: dict[str, Any] | None) -> str:
    params = parameters or {}
    safe = {
        str(key)[:40]: _safe_param_value(str(key), value)
        for key, value in list(params.items())[:12]
    }
    rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True)
    return _short_text(rendered, PARAM_LIMIT)


def truthful_claim(result_status: str, verified: bool) -> str:
    if result_status == "success" and verified:
        return "Bajarildi."
    if result_status == "failed":
        return "Bajara olmadim."
    if result_status == "needs_confirmation":
        return "Tasdiqlaysizmi?"
    return "Aniq tasdiqlay olmadim."


def infer_result_status(tool_name: str, result: Any) -> tuple[str, bool]:
    text = _squash(result).lower()
    if not text:
        return "uncertain", False

    if tool_name == "personal_briefing":
        if "[personal_operations_briefing]" in text and not any(
            marker in text for marker in ("status=failed", "status=error")
        ):
            # The verification applies to the adapter statuses and local reads.
            # External source data remains explicitly status=not_configured.
            return "success", True
        return "failed", False

    if tool_name == "send_message":
        if "verified sent" in text or "verified message sent" in text:
            return "success", True
        if any(pattern in text for pattern in _FAILURE_PATTERNS):
            return "failed", False
        return "uncertain", False

    if tool_name == "set_ui_language":
        if "ui language changed" in text or "язык интерфейса измен" in text:
            return "success", True
        return "failed", False

    if tool_name == "close_app":
        if any(
            marker in text
            for marker in (
                "verified closed",
                "closed and verified",
                "quit and verified",
                "terminated and verified",
                "already closed",
                "is not running",
            )
        ):
            return "success", True
        if text.startswith("could not") or "failed" in text or "unsupported" in text or "no application name" in text:
            return "failed", False
        return "uncertain", False

    if any(pattern in text for pattern in _CONFIRMATION_PATTERNS):
        return "needs_confirmation", False
    if any(pattern in text for pattern in _UNCERTAIN_PATTERNS):
        return "uncertain", False
    if any(pattern in text for pattern in _FAILURE_PATTERNS):
        return "failed", False
    if any(text.startswith(pattern) for pattern in _SUCCESS_STARTS):
        return "success", True
    if " saved:" in text or text.startswith("saved:"):
        return "success", True

    return "uncertain", False


def is_vague_follow_up(text: str) -> bool:
    lowered = _squash(text).lower()
    return any(re.search(pattern, lowered) for pattern in _VAGUE_PATTERNS)


def is_user_correction(text: str) -> bool:
    lowered = _squash(text).lower()
    return any(re.search(pattern, lowered) for pattern in _CORRECTION_PATTERNS)


def detect_active_app() -> str:
    if platform.system() != "Darwin":
        return ""
    try:
        proc = subprocess.run(
            [
                "osascript",
                "-e",
                'tell application "System Events" to get name of first application process whose frontmost is true',
            ],
            capture_output=True,
            text=True,
            timeout=1,
        )
        if proc.returncode == 0:
            return _short_text(proc.stdout, 80)
    except Exception:
        pass
    return ""


def classify_action(tool_name: str, parameters: dict[str, Any] | None) -> dict[str, Any]:
    """Typed, shared normalizer: map any tool call to a platform-neutral
    action-context record. Kept here (not in main.py) so recording stays a
    single shared path and the dispatcher does not accrue per-tool branches.

    Returns semantic metadata only — never a platform-specific command.
    Missing fields stay empty / None; they are never force-filled.
    """
    params = parameters or {}
    action = str(params.get("action") or "").strip().lower().replace("-", "_")
    info: dict[str, Any] = {
        "action_category": ACTION_CATEGORY_OTHER,
        "action_name": tool_name,
        "target_type": "",
        "target_name": "",
        "target_url": "",
        "target_file": "",
        "target_project": "",
        "operation": action,
        "reversible": None,
        "available_followups": [],
        "undo_action": None,
    }

    if tool_name == "open_app":
        app = _short_text(params.get("app_name"), 90)
        info.update(
            action_category=ACTION_CATEGORY_APP,
            action_name="open_app",
            target_type="application",
            target_name=app,
            operation="open",
            reversible=True,
            available_followups=["close", "focus", "reopen"],
            undo_action=({"tool": "close_app", "args": {"app_name": app}} if app else None),
        )
    elif tool_name == "close_app":
        app = _short_text(params.get("app_name"), 90)
        info.update(
            action_category=ACTION_CATEGORY_APP,
            action_name="close_app",
            target_type="application",
            target_name=app,
            operation="close",
            reversible=True,
            available_followups=["reopen"],
            undo_action=({"tool": "open_app", "args": {"app_name": app}} if app else None),
        )
    elif tool_name == "youtube_video":
        target_app = _short_text(params.get("target_app"), 90)
        url = _short_text(params.get("url"), 200)
        query = _short_text(params.get("query"), 120)
        info.update(
            action_category=ACTION_CATEGORY_MEDIA,
            action_name="play",
            target_type="media",
            target_name=query or url or target_app,
            target_url=url,
            operation=action or "play",
            reversible=True,
            available_followups=["pause", "resume", "stop", "close_tab"],
            undo_action={"tool": "media_control", "args": {"action": "pause", "target_app": target_app}},
        )
    elif tool_name == "media_control":
        target_app = _short_text(params.get("target_app"), 90)
        op = action or "pause"
        undo = None
        if op in ("pause", "media_pause", "stop", "media_stop"):
            undo = {"tool": "media_control", "args": {"action": "play_pause", "target_app": target_app}}
        info.update(
            action_category=ACTION_CATEGORY_MEDIA,
            action_name=op,
            target_type="media",
            target_name=target_app or _short_text(params.get("target_context"), 90),
            operation=op,
            reversible=True,
            available_followups=["resume", "stop"],
            undo_action=undo,
        )
    elif tool_name == "browser_control":
        browser = _normalize_browser_name(params.get("browser")) or _short_text(params.get("browser"), 90)
        url = _short_text(params.get("url"), 200)
        op = action or "go_to"
        undo = None
        if op in ("go_to", "search", "new_tab", "click", "smart_click"):
            undo = {"tool": "browser_control", "args": {"action": "back", "browser": browser}}
        info.update(
            action_category=ACTION_CATEGORY_BROWSER,
            action_name=op,
            target_type="web_page",
            target_name=browser,
            target_url=url,
            operation=op,
            reversible=op in ("go_to", "search", "new_tab"),
            available_followups=["back", "reload", "close_tab"],
            undo_action=undo,
        )
    elif tool_name in ("file_controller", "file_processor"):
        path = _short_text(params.get("file_path") or params.get("path"), 200)
        op = action or ("process" if tool_name == "file_processor" else "")
        destructive = op in DANGEROUS_FILE_ACTIONS
        info.update(
            action_category=ACTION_CATEGORY_FILE,
            action_name=op or tool_name,
            target_type="file",
            target_name=path,
            target_file=path,
            operation=op,
            # File mutations have no safe automatic undo — stay honest (None/False),
            # never fabricate an undo_action.
            reversible=(False if op == "delete" else None),
            available_followups=([] if destructive else ["edit", "move", "rename", "delete"]),
            undo_action=None,
        )
    elif tool_name == "send_message":
        platform_name = _short_text(params.get("platform"), 90)
        receiver = _short_text(params.get("receiver"), 120)
        info.update(
            action_category=ACTION_CATEGORY_MESSAGING,
            action_name="draft_or_send",
            target_type="conversation",
            target_name=receiver,
            operation=action or "send",
            reversible=False,
            available_followups=["edit", "send", "cancel"],
            undo_action=None,
        )
    elif tool_name == "reminder":
        msg = _short_text(params.get("message"), 120)
        info.update(
            action_category=ACTION_CATEGORY_REMINDER,
            action_name="create",
            target_type="reminder",
            target_name=msg,
            operation="create",
            reversible=None,
            available_followups=["reschedule", "cancel"],
            undo_action=None,
        )
    elif tool_name == "web_search":
        items = params.get("items") or []
        query = _short_text(params.get("query") or ", ".join(str(i) for i in items), 120)
        info.update(
            action_category=ACTION_CATEGORY_SEARCH,
            action_name="search",
            target_type="search",
            target_name=query,
            operation=str(params.get("mode") or "search"),
            reversible=True,
            available_followups=["open_result", "refine"],
            undo_action=None,
        )
    elif tool_name == "screen_process":
        angle = str(params.get("angle") or "screen").lower()
        if angle == "camera":
            info.update(
                action_category=ACTION_CATEGORY_CAMERA,
                action_name="camera",
                target_type="camera",
                operation="capture",
                reversible=True,
                available_followups=["reanalyze", "close"],
                undo_action={"tool": "close_camera", "args": {}},
            )
        else:
            info.update(
                action_category=ACTION_CATEGORY_SCREEN,
                action_name="screen",
                target_type="screen",
                operation="capture",
                reversible=True,
                available_followups=["reanalyze"],
                undo_action=None,
            )
    elif tool_name in ("computer_settings", "computer_control", "desktop_control"):
        info.update(
            action_category=ACTION_CATEGORY_SETTINGS,
            action_name=tool_name,
            target_type="setting",
            target_name=_short_text(params.get("description") or params.get("action"), 90),
            operation=action,
            reversible=None,
            available_followups=["toggle_back"],
            undo_action=None,
        )
    elif tool_name == "personal_briefing":
        info.update(
            action_category=ACTION_CATEGORY_BRIEFING,
            action_name="briefing",
            target_type="briefing",
            operation="briefing",
            reversible=True,
            available_followups=["refresh"],
            undo_action=None,
        )
    elif tool_name in ("system_status", "device_profile", "weather_report"):
        info.update(
            action_category=ACTION_CATEGORY_SYSTEM,
            action_name=tool_name,
            operation=action,
            reversible=True,
            available_followups=["refresh"],
        )
    elif tool_name in ("code_helper", "dev_agent", "game_updater", "flight_finder"):
        info.update(
            action_category=ACTION_CATEGORY_DEV,
            action_name=tool_name,
            operation=action,
        )

    return info


def _is_repeat_safe_record(record: ActionRecord) -> bool:
    """A repeat ('yana qil') is safe only for read-only / re-openable actions."""
    tool = record.tool_name
    if tool in DANGEROUS_REPEAT_TOOLS:
        return False
    if tool == "file_controller" and (record.operation or "") in DANGEROUS_FILE_ACTIONS:
        return False
    if tool == "file_processor":
        return True
    if tool == "browser_control":
        return (record.operation or "") in ("go_to", "search", "get_text", "get_url", "screenshot", "reload", "")
    if tool in ("media_control", "youtube_video"):
        return True
    return tool in SAFE_REPEATABLE_TOOLS


class SessionContext:
    """Runtime-only short-term action context for the current assistant process."""

    def __init__(self, max_actions: int = MAX_ACTIONS):
        self.max_actions = max_actions
        self.actions: deque[ActionRecord] = deque(maxlen=max_actions)
        self.last_browser_used = ""
        self.last_opened_app = ""
        self.last_active_app = ""
        self.last_media_search_browser_action = ""
        self.last_message_contact_action = ""
        self.last_file_action_target = ""
        # ── Per-category last-target stack ─────────────────────────────────
        # These survive deque eviction so an unrelated action in another
        # category never loses the target of an earlier category. Each holds a
        # small dict: {action_id, target, target_app, result_status, verified,
        # opened_by_us, tool_name}.
        self.category_targets: dict[str, dict[str, Any]] = {}

    def record_action(
        self,
        *,
        user_text: str,
        assistant_intent: str,
        tool_name: str,
        tool_parameters: dict[str, Any] | None,
        execution_method: str,
        result: Any,
        active_app: str = "",
        result_status: str | None = None,
        verified: bool | None = None,
        user_visible_claim: str = "",
    ) -> ActionRecord:
        status, inferred_verified = infer_result_status(tool_name, result)
        if result_status is None:
            result_status = status
        if verified is None:
            verified = inferred_verified

        params = tool_parameters or {}
        target_app, target_context = self._extract_targets(tool_name, params)
        claim = user_visible_claim or truthful_claim(result_status, verified)
        semantic = classify_action(tool_name, params)
        now = _now_iso()

        record = ActionRecord(
            timestamp=now,
            user_text=_short_text(user_text),
            assistant_intent=_short_text(assistant_intent),
            tool_name=tool_name,
            tool_parameters_summary=summarize_tool_parameters(tool_parameters),
            target_app=_short_text(target_app, 90),
            target_context=_short_text(target_context, 120),
            execution_method=_short_text(execution_method),
            result_status=result_status,
            verified=bool(verified),
            user_visible_claim=_short_text(claim),
            user_correction="",
            action_id=uuid.uuid4().hex[:8],
            action_category=semantic["action_category"],
            action_name=semantic["action_name"],
            target_type=semantic["target_type"],
            target_url=_short_text(semantic["target_url"], 200),
            target_file=_short_text(semantic["target_file"], 200),
            target_project=_short_text(semantic["target_project"], 120),
            operation=semantic["operation"],
            reversible=semantic["reversible"],
            available_followups=list(semantic["available_followups"]),
            undo_action=semantic["undo_action"],
            started_at=now,
            completed_at=now,
        )
        self.actions.append(record)
        self._update_recent_targets(record, params, active_app)
        return record

    def observe_user_text(self, user_text: str) -> bool:
        if is_user_correction(user_text):
            return self.attach_user_correction(user_text)
        return False

    def attach_user_correction(self, correction_text: str) -> bool:
        if not self.actions:
            return False
        correction = _short_text(correction_text)
        lowered = _squash(correction_text).lower()
        record = self._select_correction_target(lowered) or self.actions[-1]
        record.user_correction = correction
        self._apply_correction_to_record(record, lowered)
        return True

    def note_assistant_claim(self, claim_text: str) -> None:
        if not self.actions or not claim_text:
            return
        self.actions[-1].user_visible_claim = _short_text(claim_text)

    def resolve_follow_up(self, user_text: str, lookback: int = MAX_ACTIONS) -> dict[str, Any]:
        if not (is_vague_follow_up(user_text) or is_user_correction(user_text)):
            return {}

        lowered = _squash(user_text).lower()
        if _is_assistant_shutdown_text(lowered):
            return {}

        recent = list(self.actions)[-lookback:]

        # ── Universal high-level intents (repeat/undo/resume/back/edit/move/
        # reschedule/open-result/app-close) are resolved before the legacy
        # send/media/close chain so the same architecture covers every category.
        universal = self._resolve_universal_intent(lowered, recent)
        if universal is not None:
            return universal

        selected = self._select_relevant_record(lowered, recent)
        if not selected:
            return {
                "resolved_intent": "clarify",
                "target_context": {},
                "confidence": "low",
                "needs_confirmation": True,
                "reason": "No recent action context is available. Ask which app/browser should be targeted.",
            }

        target_context = self._resolution_target_context(selected)
        parameter_hints: dict[str, Any] = {}
        suggested_tool = selected.tool_name
        suggested_action = ""
        resolved_intent = "repeat_context"
        confidence = "medium"
        needs_confirmation = False
        reason = "Resolved from the most recent relevant action context."

        target_browser = _normalize_browser_name(selected.target_app)

        if _is_send_text(lowered) or _is_where_sent_text(lowered):
            is_where_sent = _is_where_sent_text(lowered)
            suggested_tool = "send_message"
            parameter_hints.update(self._message_hints(selected))
            resolved_intent = "message_status" if is_where_sent else "message_send_confirm"
            needs_confirmation = not is_where_sent
            confidence = "medium" if parameter_hints else "low"
            reason = (
                "Recent message context found; sending still needs confirmation/verification."
                if not is_where_sent
                else "Recent message context found for delivery-status follow-up."
            )

        elif _is_media_stop_text(lowered) and self._record_is_media_context(selected):
            suggested_tool = "media_control"
            suggested_action = "pause"
            target_app = self._media_target_app(selected)
            if target_app:
                parameter_hints["target_app"] = target_app
            if selected.target_context:
                parameter_hints["target_context"] = selected.target_context
            if _is_still_playing_text(lowered):
                parameter_hints["fallback_level"] = "stronger"
            resolved_intent = "media_pause"
            confidence = "high"
            reason = "Stop/pause follow-up matched recent YouTube/media/audio playback context."

        elif _is_close_text(lowered) and self._record_is_browser_context(selected):
            suggested_tool = "browser_control"
            suggested_action = "close_tab"
            if target_browser:
                parameter_hints["browser"] = target_browser
            resolved_intent = "browser_close"
            confidence = "high"
            reason = "Close follow-up matched recent browser/page context."

        elif _is_what_done_text(lowered):
            resolved_intent = "action_status"
            confidence = "medium"
            reason = "User asked what the previous action did."

        elif _is_media_stop_text(lowered):
            resolved_intent = "clarify_media_target"
            confidence = "low"
            needs_confirmation = True
            reason = "Stop/pause command is vague and recent context is not media playback."

        elif _is_close_text(lowered):
            resolved_intent = "clarify_close_target"
            confidence = "low"
            needs_confirmation = True
            reason = "Close command is vague and no browser/page context was found."

        return {
            "resolved_intent": resolved_intent,
            "target_context": target_context,
            "confidence": confidence,
            "reason": reason,
            "needs_confirmation": needs_confirmation,
            "source_tool": selected.tool_name,
            "suggested_tool": suggested_tool,
            "suggested_action": suggested_action,
            "target_app": selected.target_app,
            "target_context_text": selected.target_context,
            "previous_result_status": selected.result_status,
            "previous_verified": selected.verified,
            "previous_correction": selected.user_correction,
            "parameter_hints": parameter_hints,
            "must_not_claim_completed": selected.result_status != "success" or not selected.verified,
            "resolved_from_action_id": selected.action_id,
        }

    # ── Universal intent resolution ─────────────────────────────────────────
    def _make_resolution(self, selected: ActionRecord | None, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "resolved_intent": "repeat_context",
            "target_context": self._resolution_target_context(selected) if selected else {},
            "confidence": "medium",
            "reason": "",
            "needs_confirmation": False,
            "source_tool": selected.tool_name if selected else "",
            "suggested_tool": selected.tool_name if selected else "",
            "suggested_action": "",
            "target_app": selected.target_app if selected else "",
            "target_context_text": selected.target_context if selected else "",
            "previous_result_status": selected.result_status if selected else "",
            "previous_verified": selected.verified if selected else False,
            "previous_correction": selected.user_correction if selected else "",
            "parameter_hints": {},
            "must_not_claim_completed": (
                (selected.result_status != "success" or not selected.verified) if selected else True
            ),
            "resolved_from_action_id": selected.action_id if selected else "",
        }
        base.update(overrides)
        return base

    def _last_record_matching(self, recent, predicate) -> ActionRecord | None:
        for record in reversed(recent):
            if predicate(record):
                return record
        return None

    def _resolve_universal_intent(self, lowered: str, recent: list[ActionRecord]) -> dict[str, Any] | None:
        if _is_repeat_text(lowered):
            return self._resolution_repeat(recent)

        if _is_undo_text(lowered):
            return self._resolution_undo(recent)

        if _is_resume_text(lowered):
            rec = self._last_record_matching(recent, self._record_is_media_context)
            return self._resolution_media_resume(rec) if rec else None

        if _is_reschedule_text(lowered):
            rec = self._last_record_matching(
                recent, lambda r: r.action_category == ACTION_CATEGORY_REMINDER
            )
            return self._resolution_reminder_reschedule(rec)

        if _is_edit_text(lowered):
            rec = self._last_record_matching(
                recent, lambda r: r.action_category == ACTION_CATEGORY_FILE
            )
            if rec or self.last_file_action_target:
                return self._resolution_file_target(rec, "file_edit_target")
            return None

        if _is_move_text(lowered):
            rec = self._last_record_matching(
                recent, lambda r: r.action_category == ACTION_CATEGORY_FILE
            )
            if rec or self.last_file_action_target:
                return self._resolution_file_target(rec, "file_move_target")
            return None

        if _is_open_result_text(lowered):
            rec = self._last_record_matching(
                recent, lambda r: r.action_category == ACTION_CATEGORY_SEARCH
            )
            return self._resolution_open_result(rec)

        if _is_back_text(lowered):
            rec = self._last_record_matching(recent, self._record_is_browser_context)
            return self._resolution_browser_back(rec) if rec else None

        if _is_close_text(lowered) and not _is_media_stop_text(lowered):
            kind, rec = self._select_close_record(recent)
            if kind == "app" and rec is not None:
                return self._resolution_app_close(rec)
            # browser / none → fall through to the legacy browser-close/clarify chain.
            return None

        return None

    def _resolution_repeat(self, recent: list[ActionRecord]) -> dict[str, Any]:
        last = recent[-1] if recent else None
        if not last:
            return {
                "resolved_intent": "clarify",
                "target_context": {},
                "confidence": "low",
                "needs_confirmation": True,
                "reason": "Nothing to repeat yet.",
            }
        safe = _is_repeat_safe_record(last)
        return self._make_resolution(
            last,
            resolved_intent="repeat",
            suggested_tool=last.tool_name,
            confidence="high" if safe else "medium",
            needs_confirmation=not safe,
            reason=(
                "Repeating the last safe/read-only action with the same parameters."
                if safe
                else "The last action has a side effect; repeating needs confirmation."
            ),
            repeat_tool=last.tool_name,
            repeat_safe=safe,
        )

    def _resolution_undo(self, recent: list[ActionRecord]) -> dict[str, Any]:
        last = recent[-1] if recent else None
        if not last:
            return {
                "resolved_intent": "clarify",
                "target_context": {},
                "confidence": "low",
                "needs_confirmation": True,
                "reason": "Nothing to undo yet.",
            }
        if last.undo_action:
            return self._make_resolution(
                last,
                resolved_intent="undo",
                undo_action=last.undo_action,
                suggested_tool=str(last.undo_action.get("tool", "")),
                confidence="high",
                needs_confirmation=False,
                reason="Reversing the last action via its inverse operation.",
            )
        if last.action_category == ACTION_CATEGORY_MESSAGING:
            return self._make_resolution(
                last,
                resolved_intent="message_cancel",
                confidence="medium",
                needs_confirmation=False,
                reason="Cancelling the message draft; nothing was sent.",
            )
        return self._make_resolution(
            last,
            resolved_intent="undo_unsupported",
            confidence="medium",
            needs_confirmation=False,
            reason="The last action cannot be truthfully undone; do not claim it was reversed.",
        )

    def _resolution_media_resume(self, rec: ActionRecord) -> dict[str, Any]:
        target_app = self._media_target_app(rec)
        hints: dict[str, Any] = {}
        if target_app:
            hints["target_app"] = target_app
        if rec.target_context:
            hints["target_context"] = rec.target_context
        return self._make_resolution(
            rec,
            resolved_intent="media_resume",
            suggested_tool="media_control",
            suggested_action="play_pause",
            confidence="high",
            needs_confirmation=False,
            reason="Resume follow-up matched recent media/playback context.",
            parameter_hints=hints,
        )

    def _resolution_browser_back(self, rec: ActionRecord) -> dict[str, Any]:
        browser = _normalize_browser_name(rec.target_app)
        hints = {"browser": browser} if browser else {}
        return self._make_resolution(
            rec,
            resolved_intent="browser_back",
            suggested_tool="browser_control",
            suggested_action="back",
            confidence="high",
            needs_confirmation=False,
            reason="Back follow-up matched recent browser/page context.",
            parameter_hints=hints,
        )

    def _resolution_app_close(self, rec: ActionRecord) -> dict[str, Any]:
        app = rec.target_app
        opened_by_us = (
            rec.tool_name == "open_app"
            and rec.result_status == "success"
            and rec.verified
        )
        high = bool(app) and opened_by_us
        hints = {"app_name": app} if app else {}
        return self._make_resolution(
            rec,
            resolved_intent="app_close",
            suggested_tool="close_app",
            suggested_action="close",
            confidence="high" if high else ("medium" if app else "low"),
            # High confidence + app we opened → close directly; otherwise confirm.
            needs_confirmation=not high,
            reason=(
                "Close follow-up matched an app we opened this session."
                if high
                else "Close follow-up matched an app target; confirm before quitting."
            ),
            parameter_hints=hints,
        )

    def _resolution_reminder_reschedule(self, rec: ActionRecord | None) -> dict[str, Any]:
        if not rec:
            return {
                "resolved_intent": "reminder_reschedule",
                "target_context": {},
                "confidence": "low",
                "needs_confirmation": True,
                "reason": "No recent reminder found to reschedule.",
            }
        message = rec.target_context or rec.target_app
        hints = {"message": message} if message else {}
        return self._make_resolution(
            rec,
            resolved_intent="reminder_reschedule",
            suggested_tool="reminder",
            confidence="medium",
            needs_confirmation=False,
            reason=(
                "Found the recent reminder. Rescheduling creates a new reminder at the new "
                "time; the old one is not auto-removed — say so honestly."
            ),
            parameter_hints=hints,
            reschedule_recreates=True,
        )

    def _resolution_file_target(self, rec: ActionRecord | None, intent: str) -> dict[str, Any]:
        path = (rec.target_file or rec.target_context) if rec else self.last_file_action_target
        hints = {"file_path": path, "path": path} if path else {}
        return self._make_resolution(
            rec,
            resolved_intent=intent,
            suggested_tool="file_controller",
            confidence="medium",
            needs_confirmation=False,
            reason="Follow-up matched the most recent file target.",
            parameter_hints=hints,
        )

    def _resolution_open_result(self, rec: ActionRecord | None) -> dict[str, Any]:
        if not rec:
            return {
                "resolved_intent": "open_search_result",
                "target_context": {},
                "confidence": "low",
                "needs_confirmation": True,
                "reason": "No recent search to open a result from.",
            }
        return self._make_resolution(
            rec,
            resolved_intent="open_search_result",
            confidence="low",
            needs_confirmation=True,
            reason="Which search result to open is ambiguous; confirm the choice.",
            parameter_hints={"query": rec.target_context} if rec.target_context else {},
        )

    def _select_close_record(self, recent: list[ActionRecord]) -> tuple[str | None, ActionRecord | None]:
        """Pick the most recent app-open OR browser record for a 'yop'/close
        follow-up. App-open (non-browser) → app close; browser → browser close."""
        for record in reversed(recent):
            if self._record_is_browser_context(record):
                return "browser", record
            if record.tool_name in ("open_app", "close_app") and not _normalize_browser_name(record.target_app):
                return "app", record
        # Deque may have evicted the open; fall back to the per-category app target.
        app_target = self.category_targets.get(ACTION_CATEGORY_APP)
        if app_target and app_target.get("target_app") and not _normalize_browser_name(app_target["target_app"]):
            synthetic = ActionRecord(
                timestamp=_now_iso(),
                user_text="",
                assistant_intent="",
                tool_name=app_target.get("tool_name", "open_app"),
                tool_parameters_summary="",
                target_app=app_target.get("target_app", ""),
                target_context=app_target.get("target_context", ""),
                execution_method="",
                result_status=app_target.get("result_status", ""),
                verified=bool(app_target.get("verified")),
                user_visible_claim="",
                action_id=app_target.get("action_id", ""),
                action_category=ACTION_CATEGORY_APP,
            )
            return "app", synthetic
        return None, None

    def apply_context_to_tool(
        self,
        user_text: str,
        tool_name: str,
        parameters: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str]:
        args = dict(parameters or {})
        resolution = self.resolve_follow_up(user_text)
        if not resolution or resolution.get("needs_confirmation"):
            return args, ""

        notes: list[str] = []
        hints = resolution.get("parameter_hints", {})

        if tool_name == "media_control":
            if not args.get("action"):
                args["action"] = resolution.get("suggested_action") or "pause"
                notes.append(f"action={args['action']} from recent media context")
            if not args.get("target_app") and hints.get("target_app"):
                args["target_app"] = hints["target_app"]
                notes.append(f"target_app={hints['target_app']} from recent media context")
            if not args.get("target_context") and hints.get("target_context"):
                args["target_context"] = hints["target_context"]
                notes.append("target_context from recent media context")
            if not args.get("fallback_level") and hints.get("fallback_level"):
                args["fallback_level"] = hints["fallback_level"]
                notes.append("stronger fallback from user correction")

        if tool_name == "browser_control" and not args.get("browser"):
            browser = hints.get("browser") or self.last_browser_used
            if browser:
                args["browser"] = browser
                notes.append(f"browser={browser} from recent action context")

        if tool_name == "send_message":
            if not args.get("platform") and self.last_message_contact_action:
                platform = self._last_message_field("platform")
                if platform:
                    args["platform"] = platform
                    notes.append(f"platform={platform} from recent message context")
            if not args.get("receiver") and self.last_message_contact_action:
                receiver = self._last_message_field("receiver")
                if receiver:
                    args["receiver"] = receiver
                    notes.append("receiver from recent message context")

        if tool_name in {"file_processor", "file_controller"}:
            path_key = "file_path" if tool_name == "file_processor" else "path"
            if not args.get(path_key) and (hints.get(path_key) or self.last_file_action_target):
                args[path_key] = hints.get(path_key) or self.last_file_action_target
                notes.append(f"{path_key} from recent file context")

        if tool_name == "close_app" and not args.get("app_name"):
            app = hints.get("app_name") or self.category_targets.get(ACTION_CATEGORY_APP, {}).get("target_app")
            if app:
                args["app_name"] = app
                notes.append(f"app_name={app} from recent app context")

        if tool_name == "reminder" and not args.get("message") and hints.get("message"):
            args["message"] = hints["message"]
            notes.append("message from recent reminder context")

        return args, "; ".join(notes)

    def build_prompt_context(self, lookback: int = MAX_ACTIONS) -> str:
        if not self.actions:
            return "[RECENT SESSION ACTION CONTEXT]\nNo recent actions recorded yet.\n"

        lines = ["[RECENT SESSION ACTION CONTEXT]"]
        lines.append(
            "Use this runtime-only context before vague follow-up commands. "
            "Do not read this block aloud."
        )
        for idx, record in enumerate(list(self.actions)[-lookback:], 1):
            lines.append(
                f"{idx}. {record.timestamp} | user={record.user_text!r} | "
                f"intent={record.assistant_intent!r} | tool={record.tool_name} | "
                f"target_app={record.target_app or '-'} | target={record.target_context or '-'} | "
                f"status={record.result_status} | verified={record.verified} | "
                f"claim={record.user_visible_claim!r} | correction={record.user_correction or '-'}"
            )
        tracked = {
            "last_browser_used": self.last_browser_used,
            "last_opened_app": self.last_opened_app,
            "last_active_app": self.last_active_app,
            "last_media_search_browser_action": self.last_media_search_browser_action,
            "last_message_contact_action": self.last_message_contact_action,
            "last_file_action_target": self.last_file_action_target,
        }
        lines.append("Tracked targets: " + json.dumps(tracked, ensure_ascii=False))
        if self.category_targets:
            per_category = {
                category: {
                    "target": info.get("target", ""),
                    "target_app": info.get("target_app", ""),
                    "status": info.get("result_status", ""),
                    "verified": info.get("verified", False),
                }
                for category, info in self.category_targets.items()
            }
            lines.append("Category targets: " + json.dumps(per_category, ensure_ascii=False))
        return "\n".join(lines) + "\n"

    def build_user_turn_context(self, user_text: str) -> str:
        if not (is_vague_follow_up(user_text) or is_user_correction(user_text)):
            return user_text
        resolution = self.resolve_follow_up(user_text)
        return (
            "[SESSION_ACTION_CONTEXT - internal, do not read aloud]\n"
            f"{self.build_prompt_context()}"
            f"Follow-up resolution hint: {json.dumps(resolution, ensure_ascii=False)}\n"
            "[USER_COMMAND]\n"
            f"{user_text}"
        )

    def to_dicts(self) -> list[dict[str, Any]]:
        return [asdict(record) for record in self.actions]

    def _select_relevant_record(
        self,
        lowered_text: str,
        recent: list[ActionRecord],
    ) -> ActionRecord | None:
        if _is_send_text(lowered_text) or _is_where_sent_text(lowered_text):
            for record in reversed(recent):
                if record.tool_name == "send_message":
                    return record

        if _is_media_stop_text(lowered_text):
            for record in reversed(recent):
                if self._record_is_media_context(record):
                    return record

        if _is_close_text(lowered_text):
            for record in reversed(recent):
                if self._record_is_browser_context(record):
                    return record

        if _is_media_stop_text(lowered_text) or _is_close_text(lowered_text):
            for record in reversed(recent):
                if record.target_app:
                    return record

        return recent[-1] if recent else None

    def _extract_targets(self, tool_name: str, params: dict[str, Any]) -> tuple[str, str]:
        if tool_name == "open_app":
            return _short_text(params.get("app_name"), 90), ""
        if tool_name == "browser_control":
            target_app = _normalize_browser_name(params.get("browser")) or _short_text(params.get("browser"), 90) or self.last_browser_used
            context = (
                params.get("url")
                or params.get("query")
                or params.get("text")
                or params.get("description")
                or params.get("action")
            )
            return target_app, _short_text(context, 120)
        if tool_name == "send_message":
            return _short_text(params.get("platform"), 90), _short_text(params.get("receiver"), 120)
        if tool_name in {"file_processor", "file_controller"}:
            return "", _short_text(params.get("file_path") or params.get("path"), 120)
        if tool_name == "youtube_video":
            return _short_text(params.get("target_app") or "browser/media", 90), _short_text(params.get("query") or params.get("url") or params.get("action"), 120)
        if tool_name == "media_control":
            return _short_text(params.get("target_app") or self.last_active_app, 90), _short_text(params.get("target_context") or params.get("action"), 120)
        if tool_name in {"computer_settings", "computer_control"}:
            context = params.get("description") or params.get("action") or params.get("key") or params.get("keys")
            return self.last_active_app, _short_text(context, 120)
        if tool_name == "reminder":
            return "", _short_text(params.get("message"), 120)
        if tool_name == "web_search":
            items = params.get("items") or []
            query = params.get("query") or ", ".join(str(i) for i in items)
            return "", _short_text(query, 120)
        return "", _short_text(params.get("description") or params.get("action") or "", 120)

    def _update_recent_targets(
        self,
        record: ActionRecord,
        params: dict[str, Any],
        active_app: str,
    ) -> None:
        if active_app:
            self.last_active_app = active_app

        if record.tool_name == "open_app" and record.target_app:
            self.last_opened_app = record.target_app
            browser = _normalize_browser_name(record.target_app)
            if browser:
                self.last_browser_used = browser

        if record.tool_name == "browser_control":
            browser = _normalize_browser_name(params.get("browser") or record.target_app or self.last_browser_used)
            if browser in BROWSER_NAMES:
                self.last_browser_used = browser
            self.last_media_search_browser_action = f"{record.tool_name}:{record.target_app}:{record.target_context}"

        if record.tool_name == "youtube_video":
            if active_app and record.target_app == "browser/media":
                record.target_app = _short_text(active_app, 90)
            self.last_media_search_browser_action = f"{record.tool_name}:{record.target_context}"

        if record.tool_name == "media_control":
            if record.target_app or record.target_context:
                self.last_media_search_browser_action = f"{record.tool_name}:{record.target_app}:{record.target_context}"

        if record.tool_name == "send_message":
            platform = _short_text(params.get("platform"), 90)
            receiver = _short_text(params.get("receiver"), 90)
            self.last_message_contact_action = json.dumps(
                {"platform": platform, "receiver": receiver},
                ensure_ascii=False,
            )

        if record.tool_name in {"file_processor", "file_controller"} and record.target_context:
            self.last_file_action_target = record.target_context

        self._update_category_target(record)

    def _update_category_target(self, record: ActionRecord) -> None:
        """Store the last target per action category so an unrelated action in a
        different category never evicts an earlier category's target."""
        category = record.action_category
        if not category or category == ACTION_CATEGORY_OTHER:
            return
        opened_by_us = (
            record.tool_name == "open_app"
            and record.result_status == "success"
            and record.verified
        )
        target = record.target_app or record.target_file or record.target_url or record.target_context
        self.category_targets[category] = {
            "action_id": record.action_id,
            "tool_name": record.tool_name,
            "target_app": record.target_app,
            "target": target,
            "target_url": record.target_url,
            "target_file": record.target_file,
            "target_context": record.target_context,
            "operation": record.operation,
            "result_status": record.result_status,
            "verified": record.verified,
            "opened_by_us": opened_by_us,
        }

    def last_category_target(self, category: str) -> dict[str, Any]:
        return self.category_targets.get(category, {})

    def _message_hints(self, record: ActionRecord) -> dict[str, str]:
        hints = {}
        if record.target_app:
            hints["platform"] = record.target_app
        if record.target_context:
            hints["receiver"] = record.target_context
        return hints

    def _last_message_field(self, field: str) -> str:
        try:
            data = json.loads(self.last_message_contact_action or "{}")
            return _short_text(data.get(field), 90)
        except Exception:
            return ""

    def _select_correction_target(self, lowered_text: str) -> ActionRecord | None:
        recent = list(self.actions)
        if _is_still_playing_text(lowered_text):
            for record in reversed(recent):
                if self._record_is_media_control_action(record):
                    return record
            for record in reversed(recent):
                if self._record_is_media_context(record):
                    return record

        if _detect_target_app_from_text(lowered_text) or "safari emas" in lowered_text:
            for record in reversed(recent):
                if self._record_is_media_context(record) or self._record_is_browser_context(record):
                    return record

        if "yubording" in lowered_text or "send" in lowered_text:
            for record in reversed(recent):
                if record.tool_name == "send_message":
                    return record

        return recent[-1] if recent else None

    def _apply_correction_to_record(self, record: ActionRecord, lowered_text: str) -> None:
        corrected_app = _detect_target_app_from_text(lowered_text)
        if corrected_app:
            record.target_app = _short_text(corrected_app, 90)
            if self._record_is_media_context(record) and not record.target_context:
                record.target_context = "media playback"

        if "safari emas" in lowered_text and record.target_app.lower() == "safari":
            record.target_app = ""

        if _is_still_playing_text(lowered_text):
            record.result_status = "failed"
            record.verified = False
            record.user_visible_claim = truthful_claim("failed", False)

    def _record_is_media_control_action(self, record: ActionRecord) -> bool:
        haystack = self._record_haystack(record)
        return record.tool_name == "media_control" or _contains_any(
            haystack,
            (
                "media_pause",
                "media_stop",
                "play_pause",
                "pause_video",
                "mac media pause",
            ),
        )

    def _record_is_media_context(self, record: ActionRecord) -> bool:
        haystack = self._record_haystack(record)
        if record.tool_name in {"youtube_video", "media_control"}:
            return True
        if "browser/media" in record.target_app.lower():
            return True
        return _contains_any(
            haystack,
            (
                "youtube",
                "music",
                "musiqa",
                "audio",
                "video",
                "song",
                "qo'shiq",
                "qo‘shiq",
                "relaxing",
                "playback",
                "playing",
                "o'ynay",
                "o‘ynay",
                "media",
            ),
        )

    def _record_is_browser_context(self, record: ActionRecord) -> bool:
        browser = _normalize_browser_name(record.target_app)
        if browser in BROWSER_NAMES:
            return True
        if record.tool_name == "browser_control":
            return True
        if record.tool_name == "open_app" and _normalize_browser_name(record.target_app):
            return True
        return False

    def _record_haystack(self, record: ActionRecord) -> str:
        return " ".join(
            (
                record.user_text,
                record.assistant_intent,
                record.tool_name,
                record.tool_parameters_summary,
                record.target_app,
                record.target_context,
                record.execution_method,
                record.user_correction,
            )
        ).lower()

    def _media_target_app(self, record: ActionRecord) -> str:
        if record.target_app and record.target_app != "browser/media":
            return record.target_app
        if self.last_active_app:
            return self.last_active_app
        if self.last_browser_used:
            return self.last_browser_used
        return record.target_app

    def _resolution_target_context(self, record: ActionRecord) -> dict[str, str]:
        return {
            "source_tool": record.tool_name,
            "target_app": record.target_app,
            "target_context": record.target_context,
            "assistant_intent": record.assistant_intent,
            "previous_result_status": record.result_status,
            "previous_verified": str(record.verified),
            "previous_correction": record.user_correction,
        }


def resolve_followup_intent(user_text: str, session_context: SessionContext) -> dict[str, Any]:
    """Resolve a vague follow-up command against recent SessionContext actions."""
    return session_context.resolve_follow_up(user_text)
