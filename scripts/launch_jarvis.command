#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/launcher.log"
PYTHON="$PROJECT_DIR/.venv/bin/python"
QT_PREFLIGHT="$PROJECT_DIR/scripts/check_qt_runtime.py"

mkdir -p "$LOG_DIR"

{
  echo "=============================="
  echo "Launch time: $(date)"
  echo "Project dir: $PROJECT_DIR"

  if [ ! -d "$PROJECT_DIR" ]; then
    echo "ERROR: Project directory not found."
    exit 1
  fi

  cd "$PROJECT_DIR" || exit 1

  if [ ! -f "$PYTHON" ]; then
    echo "ERROR: .venv/bin/python not found."
    exit 1
  fi

  if [ ! -f "$QT_PREFLIGHT" ]; then
    echo "ERROR / ОШИБКА: Qt preflight script is missing / Скрипт проверки Qt отсутствует."
    exit 1
  fi

  echo "Python: $PYTHON"

  echo "Checking Qt runtime / Проверка среды Qt..."
  if ! "$PYTHON" -B "$QT_PREFLIGHT" --project-root "$PROJECT_DIR"; then
    echo "ERROR / ОШИБКА: JARVIS was not started because the Qt GUI check failed."
    echo "JARVIS не запущен, потому что проверка графической среды Qt завершилась ошибкой."
    echo "No package install or Qt path override was attempted."
    echo "Установка пакетов и переопределение путей Qt не выполнялись."
    echo "Review this log before changing the environment / Проверьте этот журнал перед изменением среды."
    exit 1
  fi

  echo "Starting Jarvis..."
  echo "=============================="

  exec "$PYTHON" "$PROJECT_DIR/main.py"

} >> "$LOG_FILE" 2>&1
