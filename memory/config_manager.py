import json
import sys
from pathlib import Path

from core.credential_service import load_gemini_api_key, store_gemini_api_key

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return load_gemini_api_key(legacy_path=CONFIG_FILE).ok

def save_api_keys(gemini_api_key: str) -> None:
    result = store_gemini_api_key(gemini_api_key.strip())
    if not result.ok:
        raise RuntimeError("Secure credential storage is not available.")

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    result = load_gemini_api_key(legacy_path=CONFIG_FILE)
    return result.value if result.ok else None

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)
