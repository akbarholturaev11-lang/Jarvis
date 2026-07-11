#!/bin/bash
set -u

PROJECT_DIR="$HOME/Desktop/Mark-XLVIII-AkbarCustom"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/launcher.log"
PYTHON="$PROJECT_DIR/.venv/bin/python"

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

  echo "Python: $PYTHON"

  echo "Starting Jarvis..."
  echo "=============================="

  exec "$PYTHON" "$PROJECT_DIR/main.py"

} >> "$LOG_FILE" 2>&1
