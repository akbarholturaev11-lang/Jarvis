"""core/capabilities.py — the menu of things JARVIS can do, as pickable options.

Instead of typing a command by hand, the user builds a macro by choosing from this
registry (in the desktop settings window or the phone app). Each capability maps a
bilingual label to the natural-language `phrase` that is actually sent to JARVIS,
so a macro is just one or more capability phrases composed into a single command —
"one command → several actions", generated from JARVIS's real abilities rather
than typed manually.

The phrases intentionally match routes JARVIS already understands (personal
briefing, media control, screen vision, device scan, world news, …). Capabilities
that need a free parameter (open which app? remind me what?) expose a `needs_input`
prompt so the UI can ask for that one value.
"""

from __future__ import annotations

# Each entry: id, en/ru labels, the phrase sent to JARVIS, optional input prompt.
_CAPABILITIES: list[dict] = [
    {
        "id": "briefing",
        "en": "Personal briefing",
        "ru": "Личный брифинг",
        "phrase": "statistikani ayt",
    },
    {
        "id": "home",
        "en": "I'm home",
        "ru": "Я дома",
        "phrase": "men uydaman",
    },
    {
        "id": "screen_look",
        "en": "Look at my screen",
        "ru": "Посмотри на экран",
        "phrase": "ekranimga qara",
    },
    {
        "id": "media_pause",
        "en": "Pause music",
        "ru": "Поставить музыку на паузу",
        "phrase": "musiqani to'xtat",
    },
    {
        "id": "world_news",
        "en": "World news",
        "ru": "Мировые новости",
        "phrase": "dunyo yangiliklari",
    },
    {
        "id": "device_scan",
        "en": "Re-scan this device",
        "ru": "Пересканировать устройство",
        "phrase": "qurilmani qayta tekshir",
    },
    {
        "id": "projects",
        "en": "Check my projects",
        "ru": "Проверить мои проекты",
        "phrase": "loyihalarimni tekshir",
    },
    {
        "id": "open_app",
        "en": "Open an app",
        "ru": "Открыть приложение",
        "phrase": "{value} ni och",
        "needs_input": {"en": "App name", "ru": "Название приложения"},
    },
    {
        "id": "web_search",
        "en": "Search the web",
        "ru": "Искать в интернете",
        "phrase": "internetdan {value} ni qidir",
        "needs_input": {"en": "Search query", "ru": "Поисковый запрос"},
    },
    {
        "id": "reminder",
        "en": "Set a reminder",
        "ru": "Поставить напоминание",
        "phrase": "eslatma qo'y: {value}",
        "needs_input": {"en": "Reminder text", "ru": "Текст напоминания"},
    },
]


def list_capabilities(lang: str = "en") -> list[dict]:
    """Return capabilities with a resolved label for the given UI language.

    Shape per item: {id, label, phrase, needs_input(bool), input_label}.
    """
    lang = "ru" if str(lang).lower().startswith("ru") else "en"
    out: list[dict] = []
    for c in _CAPABILITIES:
        ni = c.get("needs_input")
        out.append({
            "id": c["id"],
            "label": c.get(lang) or c["en"],
            "phrase": c["phrase"],
            "needs_input": bool(ni),
            "input_label": (ni.get(lang) or ni.get("en")) if ni else "",
        })
    return out


def get_capability(cap_id: str) -> dict | None:
    for c in _CAPABILITIES:
        if c["id"] == cap_id:
            return c
    return None


def build_phrase(cap_id: str, value: str = "") -> str:
    """Resolve one capability's phrase, substituting a user value if it needs one."""
    c = get_capability(cap_id)
    if not c:
        return ""
    phrase = c["phrase"]
    if "{value}" in phrase:
        return phrase.replace("{value}", (value or "").strip()).strip()
    return phrase


def compose_macro(steps: list[dict]) -> str:
    """Compose a macro command from steps [{id, value?}, ...] into one instruction.

    Steps are joined with the Uzbek conjunction so JARVIS performs each action:
    e.g. [screen_look, media_pause] → "ekranimga qara va musiqani to'xtat".
    """
    parts: list[str] = []
    for step in steps or []:
        if isinstance(step, str):
            phrase = build_phrase(step)
        else:
            phrase = build_phrase(step.get("id", ""), step.get("value", ""))
        phrase = (phrase or "").strip()
        if phrase:
            parts.append(phrase)
    return ", va ".join(parts)
