#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DATA_DIR="$SCRIPT_DIR/data"
STATUS_FILE="$DATA_DIR/backup-status.json"
PID_FILE="$DATA_DIR/backup.pid"
RCLONE_LOG="$DATA_DIR/rclone-backup.log"

if [[ -f "$ENV_FILE" ]]; then
  # .env is shared with Docker Compose and is also sourced by these host scripts.
  # Keep it shell-compatible and quote values that contain spaces.
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

BACKUP_USER="${BACKUP_USER:-${SUDO_USER:-}}"
BACKUP_ROOT="${BACKUP_ROOT:-}"
APPDATA="${APPDATA:-}"
MEDIA_ROOT="${MEDIA_ROOT:-}"
RCLONE_ENABLED="${RCLONE_ENABLED:-false}"
RCLONE_REMOTE="${RCLONE_REMOTE:-gdrive}"
RCLONE_DESTINATION="${RCLONE_DESTINATION:-Backups/server-config}"
BACKUP_USER_SSH="${BACKUP_USER_SSH:-false}"
BACKUP_USER_CONFIG="${BACKUP_USER_CONFIG:-false}"

is_true() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

die() {
  echo "Error: $*" >&2
  exit 1
}

[[ -n "$BACKUP_USER" ]] || die "BACKUP_USER is not set in .env."
[[ -n "$BACKUP_ROOT" ]] || die "BACKUP_ROOT is not set in .env."
id "$BACKUP_USER" >/dev/null 2>&1 || die "BACKUP_USER '$BACKUP_USER' does not exist."

mkdir -p "$DATA_DIR" "$BACKUP_ROOT"

if [[ -n "$APPDATA" ]]; then
  [[ -d "$APPDATA" ]] || die "APPDATA does not exist: $APPDATA"

  appdata_real="$(realpath -m "$APPDATA")"
  backup_root_real="$(realpath -m "$BACKUP_ROOT")"

  case "$backup_root_real/" in
    "$appdata_real/"*)
      die "BACKUP_ROOT must not be inside APPDATA (this would recursively back up backups)."
      ;;
  esac
fi

if is_true "$RCLONE_ENABLED"; then
  command -v rclone >/dev/null 2>&1 || die "RCLONE_ENABLED=true but rclone is not installed."
  [[ -n "$RCLONE_REMOTE" ]] || die "RCLONE_REMOTE is empty."
fi

TIMESTAMP="$(date +"%Y-%m-%d_%H-%M-%S")"
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"
ARCHIVE_PATH="$BACKUP_DIR.tar.gz"

echo "$$" > "$PID_FILE"

cleanup_pid() {
  rm -f "$PID_FILE"
}
trap cleanup_pid EXIT

get_status_field() {
  local field="$1"
  local fallback="$2"

  python3 - "$STATUS_FILE" "$field" "$fallback" <<'PY'
import json
import os
import sys

path, field, fallback = sys.argv[1], sys.argv[2], sys.argv[3]

try:
    if not os.path.exists(path):
        print(fallback)
        raise SystemExit

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    value = data.get(field, fallback)
    print(value if value not in (None, "") else fallback)
except Exception:
    print(fallback)
PY
}

write_status() {
  local last_backup="$1"
  local archive="$2"
  local size="$3"
  local status="$4"
  local phase="$5"
  local message="$6"

  python3 - \
    "$STATUS_FILE" \
    "$last_backup" \
    "$archive" \
    "$size" \
    "$status" \
    "$phase" \
    "$message" <<'PY'
import json
import os
import sys
from datetime import datetime

path, last_backup, archive, size, status, phase, message = sys.argv[1:]

data = {
    "last_backup": last_backup,
    "archive": archive,
    "size": size,
    "status": status,
    "phase": phase,
    "message": message,
    "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}

os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"

with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)

os.replace(tmp, path)
PY
}

PREV_LAST_BACKUP="$(get_status_field "last_backup" "Never")"
PREV_ARCHIVE="$(get_status_field "archive" "None")"
PREV_SIZE="$(get_status_field "size" "Unknown")"

CURRENT_LAST_BACKUP="$PREV_LAST_BACKUP"
CURRENT_ARCHIVE="$PREV_ARCHIVE"
CURRENT_SIZE="$PREV_SIZE"

on_error() {
  local exit_code="$?"

  write_status \
    "$CURRENT_LAST_BACKUP" \
    "$CURRENT_ARCHIVE" \
    "$CURRENT_SIZE" \
    "failed" \
    "failed" \
    "Backup failed"

  echo
  echo "Backup failed with exit code $exit_code"
  exit "$exit_code"
}

trap on_error ERR

mkdir -p "$BACKUP_DIR"

write_status \
  "$PREV_LAST_BACKUP" \
  "$PREV_ARCHIVE" \
  "$PREV_SIZE" \
  "running" \
  "backup" \
  "Creating backup archive"

echo "Creating backup: $BACKUP_DIR"

# Docker/application data
if [[ -n "$APPDATA" ]]; then
  echo "Backing up application data: $APPDATA"
  cp -a "$APPDATA" "$BACKUP_DIR/appdata"
else
  echo "APPDATA is empty; skipping application data."
fi

# Common system configuration
mkdir -p "$BACKUP_DIR/etc"

cp /etc/fstab "$BACKUP_DIR/etc/fstab" 2>/dev/null || true
cp /etc/crontab "$BACKUP_DIR/etc/crontab" 2>/dev/null || true
cp /etc/hosts "$BACKUP_DIR/etc/hosts" 2>/dev/null || true
cp /etc/samba/smb.conf "$BACKUP_DIR/etc/smb.conf" 2>/dev/null || true
cp /etc/docker/daemon.json "$BACKUP_DIR/etc/docker-daemon.json" 2>/dev/null || true

# Custom systemd units and overrides
cp -a /etc/systemd/system "$BACKUP_DIR/systemd-system" 2>/dev/null || true

# Optional user SSH/config backups. These may contain credentials.
if is_true "$BACKUP_USER_SSH"; then
  cp -a "/home/$BACKUP_USER/.ssh" "$BACKUP_DIR/user-ssh" 2>/dev/null || true
fi

if is_true "$BACKUP_USER_CONFIG"; then
  cp -a "/home/$BACKUP_USER/.config" "$BACKUP_DIR/user-config" 2>/dev/null || true
fi

# Docker inventory
if command -v docker >/dev/null 2>&1; then
  docker ps -a > "$BACKUP_DIR/docker-containers.txt" 2>&1 || true
  docker images > "$BACKUP_DIR/docker-images.txt" 2>&1 || true
fi

# Enabled system services
if command -v systemctl >/dev/null 2>&1; then
  systemctl list-unit-files \
    --state=enabled \
    --type=service \
    --no-pager \
    > "$BACKUP_DIR/enabled-services.txt" 2>&1 || true
fi

# User crontab
sudo -u "$BACKUP_USER" crontab -l \
  > "$BACKUP_DIR/crontab-user.txt" 2>/dev/null || true

# Tailscale inventory, if installed
if command -v tailscale >/dev/null 2>&1; then
  tailscale status > "$BACKUP_DIR/tailscale-status.txt" 2>/dev/null || true
fi

# Storage inventory
lsblk > "$BACKUP_DIR/lsblk.txt" 2>&1 || true
df -h > "$BACKUP_DIR/df.txt" 2>&1 || true
mount | grep mergerfs > "$BACKUP_DIR/mergerfs.txt" 2>/dev/null || true

# Media inventory: directory names only, not media file contents.
if [[ -n "$MEDIA_ROOT" && -d "$MEDIA_ROOT" ]]; then
  {
    for category in Movies Shows Audiobooks Podcasts Music Books; do
      echo "=== ${category^^} ==="
      find "$MEDIA_ROOT/$category" \
        -mindepth 1 \
        -maxdepth 1 \
        -type d \
        2>/dev/null | sort || true
      echo
    done
  } > "$BACKUP_DIR/media-inventory.txt"
fi

# System information
{
  echo "Hostname:"
  hostname

  echo
  echo "Kernel:"
  uname -a

  echo
  echo "Operating System:"
  cat /etc/os-release 2>/dev/null || true
} > "$BACKUP_DIR/system-info.txt"

# Include the host-side scripts used to create/manage this backup.
mkdir -p "$BACKUP_DIR/backup-tool"
cp "$SCRIPT_DIR/backup-script.sh" "$BACKUP_DIR/backup-tool/"
cp "$SCRIPT_DIR/stop-backup.sh" "$BACKUP_DIR/backup-tool/"

# Give the configured user ownership of the staged backup.
chown -R "$BACKUP_USER:" "$BACKUP_DIR"

echo
echo "Backup staging complete."
echo "$BACKUP_DIR"

echo
echo "Uncompressed backup size:"
du -sh "$BACKUP_DIR"

echo
echo "Compressing backup..."

tar -czf "$ARCHIVE_PATH" \
  -C "$BACKUP_ROOT" \
  "$TIMESTAMP"

rm -rf "$BACKUP_DIR"

chown "$BACKUP_USER:" "$ARCHIVE_PATH"

echo
echo "Backup archive:"
echo "$ARCHIVE_PATH"

echo
echo "Backup size:"
ARCHIVE_SIZE="$(du -sh "$ARCHIVE_PATH" | cut -f1)"
echo "$ARCHIVE_SIZE"

CURRENT_ARCHIVE="$(basename "$ARCHIVE_PATH")"
CURRENT_SIZE="$ARCHIVE_SIZE"

if is_true "$RCLONE_ENABLED"; then
  write_status \
    "$PREV_LAST_BACKUP" \
    "$CURRENT_ARCHIVE" \
    "$CURRENT_SIZE" \
    "running" \
    "uploading" \
    "Uploading backup with rclone"

  RCLONE_TARGET="${RCLONE_REMOTE}:${RCLONE_DESTINATION}"

  echo
  echo "Uploading backup to: $RCLONE_TARGET"

  if sudo -u "$BACKUP_USER" rclone copy \
    "$ARCHIVE_PATH" \
    "$RCLONE_TARGET" \
    -P \
    >"$RCLONE_LOG" 2>&1
  then
    CURRENT_LAST_BACKUP="$(date '+%Y-%m-%d %H:%M:%S')"

    write_status \
      "$CURRENT_LAST_BACKUP" \
      "$CURRENT_ARCHIVE" \
      "$CURRENT_SIZE" \
      "completed" \
      "idle" \
      "Backup and rclone upload completed"

    echo
    echo "Remote upload complete."
  else
    write_status \
      "$PREV_LAST_BACKUP" \
      "$CURRENT_ARCHIVE" \
      "$CURRENT_SIZE" \
      "failed" \
      "failed" \
      "Remote upload failed"

    echo
    echo "Remote upload failed."
    echo "Check log:"
    echo "cat $RCLONE_LOG"

    exit 1
  fi
else
  CURRENT_LAST_BACKUP="$(date '+%Y-%m-%d %H:%M:%S')"

  write_status \
    "$CURRENT_LAST_BACKUP" \
    "$CURRENT_ARCHIVE" \
    "$CURRENT_SIZE" \
    "completed" \
    "idle" \
    "Local backup completed"

  echo
  echo "Rclone upload disabled; local backup complete."
fi

echo
echo "Backup status:"
cat "$STATUS_FILE"
