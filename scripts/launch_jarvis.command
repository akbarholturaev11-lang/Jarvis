#!/bin/bash
set -u

PROJECT_DIR="$HOME/Desktop/Mark-XLVIII-AkbarCustom"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/launcher.log"

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

  if [ ! -f ".venv/bin/python" ]; then
    echo "ERROR: .venv/bin/python not found."
    exit 1
  fi

  echo "Python: $PROJECT_DIR/.venv/bin/python"
  echo "Starting Jarvis..."
  echo "=============================="

  exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/main.py"

} >> "$LOG_FILE" 2>&1
