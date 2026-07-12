"""core/macros.py — user-defined command macros (one command → several actions).

Persisted in config/macros.json (non-secret local user data). Each macro bundles
capability steps into a single named command whose composed phrase is sent to
JARVIS. Shared by the desktop settings window, the phone app, and voice so the
same macro works everywhere.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

from core.app_paths import resolve_app_paths
from core.capabilities import compose_macro

BASE_DIR = Path(__file__).resolve().parent.parent
MACROS_FILE = (
    resolve_app_paths().config_dir / "macros.json"
    if getattr(sys, "frozen", False)
    else BASE_DIR / "config" / "macros.json"
)


def load_macros() -> list[dict]:
    try:
        data = json.loads(MACROS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict) and str(m.get("name", "")).strip()]
    except Exception:
        pass
    return []


def save_macros(macros: list[dict]) -> None:
    MACROS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MACROS_FILE.write_text(
        json.dumps(macros, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _normalize(macro: dict) -> dict:
    steps_raw = macro.get("steps") or []
    steps: list[dict] = []
    for s in steps_raw:
        if isinstance(s, str):
            steps.append({"id": s, "value": ""})
        elif isinstance(s, dict) and s.get("id"):
            steps.append({"id": str(s["id"]), "value": str(s.get("value", ""))})
    phrase = str(macro.get("phrase") or "").strip() or compose_macro(steps)
    return {
        "id": str(macro.get("id") or secrets.token_hex(6)),
        "name": str(macro.get("name", "")).strip(),
        "steps": steps,
        "phrase": phrase,
    }


def set_macros(macros: list[dict]) -> list[dict]:
    """Replace the whole macro list (used by the settings UIs). Returns normalized."""
    norm = [
        _normalize(m)
        for m in (macros or [])
        if isinstance(m, dict) and str(m.get("name", "")).strip()
    ]
    save_macros(norm)
    return norm


def add_macro(name: str, steps: list, phrase: str = "") -> list[dict]:
    macros = load_macros()
    macros.append(_normalize({"name": name, "steps": steps, "phrase": phrase}))
    save_macros(macros)
    return macros


def remove_macro(macro_id: str) -> list[dict]:
    macros = [m for m in load_macros() if m.get("id") != macro_id]
    save_macros(macros)
    return macros
