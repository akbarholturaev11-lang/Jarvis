from __future__ import annotations

import json
import platform
import re
import subprocess
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


MAX_ACTIONS = 5
SUMMARY_LIMIT = 160
PARAM_LIMIT = 220

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
    "active browser",
    "all browsers closed",
)

_VAGUE_PATTERNS = (
    r"\bo['‘`]?chir\b",
    r"\bto['‘`]?xtat\b",
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
    r"\bзакрой\b",
    r"\bотправь\b",
    r"\bотмени\b",
)

_CORRECTION_PATTERNS = (
    r"\byo['‘`]?q[, ]+noto['‘`]?g['‘`]?ri\b",
    r"\bnoto['‘`]?g['‘`]?ri\b",
    r"\bboshqa joyga yubording\b",
    r"\bsafari emas\b",
    r"\bmen buni demadim\b",
    r"\bbu ishlamadi\b",
    r"\bwrong\b",
    r"\bnot that\b",
    r"\bthat did not work\b",
    r"\bdidn['’]?t work\b",
    r"\bне так\b",
    r"\bнеправильно\b",
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
    return "Aniq tasdiqlay olmadim."


def infer_result_status(tool_name: str, result: Any) -> tuple[str, bool]:
    text = _squash(result).lower()
    if not text:
        return "uncertain", False

    if tool_name == "send_message":
        if "verified sent" in text or "verified message sent" in text:
            return "success", True
        if any(pattern in text for pattern in _FAILURE_PATTERNS):
            return "failed", False
        return "uncertain", False

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

        target_app, target_context = self._extract_targets(tool_name, tool_parameters or {})
        claim = user_visible_claim or truthful_claim(result_status, verified)

        record = ActionRecord(
            timestamp=_now_iso(),
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
        )
        self.actions.append(record)
        self._update_recent_targets(record, tool_parameters or {}, active_app)
        return record

    def observe_user_text(self, user_text: str) -> bool:
        if is_user_correction(user_text):
            return self.attach_user_correction(user_text)
        return False

    def attach_user_correction(self, correction_text: str) -> bool:
        if not self.actions:
            return False
        correction = _short_text(correction_text)
        for record in reversed(self.actions):
            if not record.user_correction:
                record.user_correction = correction
                return True
        self.actions[-1].user_correction = correction
        return True

    def note_assistant_claim(self, claim_text: str) -> None:
        if not self.actions or not claim_text:
            return
        self.actions[-1].user_visible_claim = _short_text(claim_text)

    def resolve_follow_up(self, user_text: str, lookback: int = MAX_ACTIONS) -> dict[str, Any]:
        if not is_vague_follow_up(user_text):
            return {}

        lowered = _squash(user_text).lower()
        recent = list(self.actions)[-lookback:]
        selected = self._select_relevant_record(lowered, recent)
        if not selected:
            return {
                "needs_confirmation": True,
                "reason": "No recent action context is available.",
            }

        suggested_tool = selected.tool_name
        parameter_hints: dict[str, Any] = {}

        target_browser = _normalize_browser_name(selected.target_app)
        if selected.tool_name == "browser_control" or target_browser:
            suggested_tool = "browser_control"
            if target_browser:
                parameter_hints["browser"] = target_browser
        elif selected.tool_name == "send_message":
            suggested_tool = "send_message"
            parameter_hints.update(self._message_hints(selected))
        elif selected.tool_name in {"file_processor", "file_controller"}:
            suggested_tool = selected.tool_name
            if self.last_file_action_target:
                parameter_hints["path"] = self.last_file_action_target
                parameter_hints["file_path"] = self.last_file_action_target

        return {
            "needs_confirmation": False,
            "source_tool": selected.tool_name,
            "suggested_tool": suggested_tool,
            "target_app": selected.target_app,
            "target_context": selected.target_context,
            "previous_result_status": selected.result_status,
            "previous_verified": selected.verified,
            "previous_correction": selected.user_correction,
            "parameter_hints": parameter_hints,
            "must_not_claim_completed": selected.result_status != "success" or not selected.verified,
        }

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
            if not args.get(path_key) and self.last_file_action_target:
                args[path_key] = self.last_file_action_target
                notes.append(f"{path_key} from recent file context")

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
        if any(word in lowered_text for word in ("yubor", "send", "отправ")):
            for record in reversed(recent):
                if record.tool_name == "send_message":
                    return record

        if any(word in lowered_text for word in ("qayerga yubording", "where did you send")):
            for record in reversed(recent):
                if record.tool_name == "send_message":
                    return record

        if any(word in lowered_text for word in ("o'chir", "o‘chir", "to'xtat", "to‘xtat", "stop", "close", "закрой", "останови")):
            for record in reversed(recent):
                if record.tool_name in {"browser_control", "youtube_video"} or record.target_app in BROWSER_NAMES:
                    return record
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
            return "browser/media", _short_text(params.get("query") or params.get("url") or params.get("action"), 120)
        if tool_name in {"computer_settings", "computer_control"}:
            context = params.get("description") or params.get("action") or params.get("key") or params.get("keys")
            return self.last_active_app, _short_text(context, 120)
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
            self.last_media_search_browser_action = f"{record.tool_name}:{record.target_context}"

        if record.tool_name == "send_message":
            platform = _short_text(params.get("platform"), 90)
            receiver = _short_text(params.get("receiver"), 90)
            self.last_message_contact_action = json.dumps(
                {"platform": platform, "receiver": receiver},
                ensure_ascii=False,
            )

        if record.tool_name in {"file_processor", "file_controller"} and record.target_context:
            self.last_file_action_target = record.target_context

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
