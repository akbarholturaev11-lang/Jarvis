#!/usr/bin/env bash
set -euo pipefail
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$ROOT_DIR/config/briefing_sources.json"
ENV_FILE="$ROOT_DIR/config/local_env.zsh"

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "Xato: Python 3 topilmadi." >&2
  exit 1
fi

for relative_path in \
  config/briefing_sources.json \
  config/local_env.zsh \
  config/.briefing_sources.json.setup-check \
  config/.local_env.zsh.setup-check
do
  if ! git -C "$ROOT_DIR" check-ignore -q -- "$relative_path"; then
    echo "Xato: $relative_path .gitignore bilan himoyalanmagan." >&2
    exit 1
  fi
  if git -C "$ROOT_DIR" ls-files --error-unmatch -- "$relative_path" >/dev/null 2>&1; then
    echo "Xato: $relative_path Git tomonidan kuzatilmoqda; token yozilmadi." >&2
    exit 1
  fi
done

printf "Zerno API URL: "
IFS= read -r API_URL
if [ -z "${API_URL//[[:space:]]/}" ]; then
  echo "Xato: API URL bo‘sh bo‘lishi mumkin emas." >&2
  exit 1
fi

printf "Zerno API token (ekranda ko‘rinmaydi): "
IFS= read -r -s API_TOKEN
printf "\n"
if [ -z "$API_TOKEN" ]; then
  echo "Xato: API token bo‘sh bo‘lishi mumkin emas." >&2
  exit 1
fi
case "$API_TOKEN" in
  *$'\r'*|*$'\n'*)
    echo "Xato: API token yangi qator belgisini o‘z ichiga olmasligi kerak." >&2
    exit 1
    ;;
esac

umask 077
export ZERNO_SETUP_TOKEN="$API_TOKEN"
export ZERNO_SETUP_URL="$API_URL"

"$PYTHON_BIN" - "$CONFIG_FILE" "$ENV_FILE" <<'PY'
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit


config_path = Path(sys.argv[1])
env_path = Path(sys.argv[2])
api_url = os.environ.pop("ZERNO_SETUP_URL", "").strip()
token = os.environ.pop("ZERNO_SETUP_TOKEN", "")
if not token:
    raise SystemExit("Xato: token child process ichiga uzatilmadi.")
if not api_url or "paste_zerno_api_url_here" in api_url.casefold():
    raise SystemExit("Xato: haqiqiy Zerno API URL kiriting.")

try:
    parsed_url = urlsplit(api_url)
    hostname = (parsed_url.hostname or "").casefold()
except ValueError:
    raise SystemExit("Xato: Zerno API URL formati noto‘g‘ri.") from None
if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
    raise SystemExit("Xato: Zerno API URL to‘liq http(s) manzil bo‘lishi kerak.")
if parsed_url.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
    raise SystemExit("Xato: internet Zerno API uchun HTTPS URL kerak.")
if parsed_url.username or parsed_url.password:
    raise SystemExit("Xato: credentialni Zerno API URL ichiga yozmang.")


def looks_sensitive(value: object) -> bool:
    raw = str(value)
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.casefold()).strip("_")
    compact = normalized.replace("_", "")
    segments = set(normalized.split("_"))
    return compact == "headers" or any(
        term in compact
        for term in (
            "token",
            "secret",
            "password",
            "passwd",
            "credential",
            "authorization",
            "cookie",
            "apikey",
            "accesskey",
            "privatekey",
        )
    ) or bool(segments & {"auth", "authentication", "oauth", "jwt", "signature", "sig"})


if any(looks_sensitive(key) for key, _ in parse_qsl(parsed_url.query, keep_blank_values=True)):
    raise SystemExit("Xato: API tokenni URL query ichiga yozmang.")


def atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        if temporary.exists():
            temporary.unlink()


if config_path.exists():
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"Xato: mavjud briefing_sources.json yaroqsiz; fayl o‘zgartirilmadi ({exc.__class__.__name__})."
        ) from None
else:
    document = {"sources": []}

if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
    raise SystemExit("Xato: briefing_sources.json ichida sources ro‘yxati bo‘lishi kerak.")

zerno_source = next(
    (
        source
        for source in document["sources"]
        if isinstance(source, dict) and str(source.get("type", "")).casefold() == "zerno"
    ),
    None,
)
if zerno_source is None:
    zerno_source = {}
    document["sources"].append(zerno_source)

for unsafe_key in list(zerno_source):
    if looks_sensitive(unsafe_key):
        zerno_source.pop(unsafe_key, None)
zerno_source.update(
    {
        "name": "Zerno Operations Hub",
        "type": "zerno",
        "enabled": True,
        "api_base_url": api_url,
        "token_env": "ZERNO_API_TOKEN",
    }
)
atomic_write(config_path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
export_pattern = re.compile(r"^\s*(?:export\s+)?ZERNO_API_TOKEN=")
kept_lines = [line for line in existing_lines if not export_pattern.match(line)]
kept_lines.append(f"export ZERNO_API_TOKEN={shlex.quote(token)}")
atomic_write(env_path, "\n".join(kept_lines).rstrip() + "\n")
PY

unset ZERNO_SETUP_TOKEN ZERNO_SETUP_URL API_TOKEN API_URL

echo "Zerno sozlamalari xavfsiz lokal fayllarga saqlandi."
echo "Keyingi buyruqlar:"
echo "  cd \"$ROOT_DIR\""
echo "  source config/local_env.zsh"
echo "  python scripts/check_zerno_stats.py"
echo "  python main.py"
