#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DATA_DIR="$SCRIPT_DIR/data"
STATUS_FILE="$DATA_DIR/backup-status.json"
LOCK_FILE="$DATA_DIR/backup.lock"
PID_FILE="$DATA_DIR/backup.pid"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

mkdir -p "$DATA_DIR"

echo "Stopping server backup..."

collect_descendants() {
  local parent_pid="$1"
  local child

  while read -r child; do
    [[ -n "$child" ]] || continue
    collect_descendants "$child"
    echo "$child"
  done < <(pgrep -P "$parent_pid" 2>/dev/null || true)
}

terminate_backup_tree() {
  local root_pid="$1"
  local args
  local descendants

  if ! kill -0 "$root_pid" 2>/dev/null; then
    return 0
  fi

  args="$(ps -p "$root_pid" -o args= 2>/dev/null || true)"

  if [[ "$args" != *"$SCRIPT_DIR/backup-script.sh"* ]]; then
    echo "Ignoring stale PID $root_pid; it is not running this backup script."
    return 0
  fi

  descendants="$(collect_descendants "$root_pid")"

  # Stop children first so rclone or other subprocesses do not survive.
  if [[ -n "$descendants" ]]; then
    while read -r pid; do
      kill -TERM "$pid" 2>/dev/null || true
    done <<< "$descendants"
  fi

  kill -TERM "$root_pid" 2>/dev/null || true
  sleep 2

  if [[ -n "$descendants" ]]; then
    while read -r pid; do
      kill -KILL "$pid" 2>/dev/null || true
    done <<< "$descendants"
  fi

  kill -KILL "$root_pid" 2>/dev/null || true
}

if [[ -f "$PID_FILE" ]]; then
  BACKUP_PID="$(cat "$PID_FILE" 2>/dev/null || true)"

  if [[ "$BACKUP_PID" =~ ^[0-9]+$ ]]; then
    terminate_backup_tree "$BACKUP_PID"
  fi
else
  # Fallback for a backup started before the PID-file version of the script.
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$$" ]] && continue
    terminate_backup_tree "$pid"
  done < <(pgrep -f "$SCRIPT_DIR/backup-script.sh" 2>/dev/null || true)
fi

rm -f "$LOCK_FILE" "$PID_FILE"

python3 - "$STATUS_FILE" <<'PY'
import json
import os
import sys
from datetime import datetime

path = sys.argv[1]

data = {
    "last_backup": "Never",
    "archive": "None",
    "size": "Unknown",
}

if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data.update(json.load(f))
    except Exception:
        pass

data.pop("error", None)
data.pop("failed_at", None)

data["status"] = "stopped"
data["phase"] = "stopped"
data["running"] = False
data["message"] = "Backup was stopped manually"
data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"

with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

os.replace(tmp, path)
PY

echo "Backup stopped."
