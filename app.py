from flask import Flask, jsonify, Response, request, redirect
import json
import os
import shlex
import subprocess
import threading
from datetime import datetime

app = Flask(__name__)

DATA_DIR = "/data"
STATUS_FILE = os.path.join(DATA_DIR, "backup-status.json")
LOCK_FILE = os.path.join(DATA_DIR, "backup.lock")

SSH_KEY = os.getenv("SSH_KEY", "/ssh/id_ed25519")
SSH_HOST = os.getenv("SSH_HOST", "host.docker.internal")
SSH_USER = os.getenv("SSH_USER", "user")
SSH_STRICT_HOST_KEY_CHECKING = os.getenv(
    "SSH_STRICT_HOST_KEY_CHECKING", "accept-new"
)
SSH_KNOWN_HOSTS_FILE = os.getenv(
    "SSH_KNOWN_HOSTS_FILE", "/data/known_hosts"
)

HOST_PROJECT_DIR = os.getenv(
    "HOST_PROJECT_DIR",
    f"/home/{SSH_USER}/glance-backup-status",
)

VALID_WIDGET_THEMES = {
    "custom",
    "default-dark",
    "default-light",
    "auto",
}

WIDGET_THEME = os.getenv("WIDGET_THEME", "default-dark").strip().lower()
if WIDGET_THEME not in VALID_WIDGET_THEMES:
    WIDGET_THEME = "default-dark"

os.makedirs(DATA_DIR, exist_ok=True)


def host_script_command(script_name):
    script_path = shlex.quote(
        os.path.join(HOST_PROJECT_DIR, script_name)
    )
    return f"sudo -n /bin/bash {script_path}"


BACKUP_COMMAND = host_script_command("backup-script.sh")
STOP_COMMAND = host_script_command("stop-backup.sh")


def now_string():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def default_status():
    return {
        "last_backup": "Never",
        "archive": "None",
        "size": "Unknown",
        "status": "unknown",
        "phase": "unknown",
        "message": "No backup status has been recorded yet",
    }


def read_status():
    if not os.path.exists(STATUS_FILE):
        return default_status()

    with open(STATUS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def write_status(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_file = STATUS_FILE + ".tmp"

    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    os.replace(tmp_file, STATUS_FILE)


def remove_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def backup_is_running():
    if not os.path.exists(LOCK_FILE):
        return False

    try:
        data = read_status()
        status = str(data.get("status", "")).lower()
        phase = str(data.get("phase", "")).lower()

        terminal_statuses = {"completed", "failed", "error", "stopped"}
        terminal_phases = {"idle", "failed", "stopped"}

        if status in terminal_statuses and phase in terminal_phases:
            remove_lock()
            return False
    except Exception:
        # If the status file cannot be read, keep the lock rather than
        # accidentally starting a second backup.
        pass

    return True


def acquire_lock():
    os.makedirs(DATA_DIR, exist_ok=True)

    try:
        fd = os.open(
            LOCK_FILE,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o644,
        )
    except FileExistsError:
        return False

    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(now_string())

    return True


def run_ssh_command(command):
    return subprocess.run(
        [
            "ssh",
            "-i",
            SSH_KEY,
            "-o",
            "BatchMode=yes",
            "-o",
            f"StrictHostKeyChecking={SSH_STRICT_HOST_KEY_CHECKING}",
            "-o",
            f"UserKnownHostsFile={SSH_KNOWN_HOSTS_FILE}",
            "-o",
            "ConnectTimeout=10",
            f"{SSH_USER}@{SSH_HOST}",
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=None,
    )


def run_backup_background():
    try:
        current = read_status()
        current.pop("error", None)
        current.pop("failed_at", None)
        current["status"] = "running"
        current["phase"] = "backup"
        current["started_at"] = now_string()
        current["message"] = "Backup started from Glance"
        write_status(current)

        result = run_ssh_command(BACKUP_COMMAND)

        if result.returncode != 0:
            current = read_status()
            current_status = str(current.get("status", "")).lower()
            current_phase = str(current.get("phase", "")).lower()

            if (
                current_status in {"stopping", "stopped"}
                or current_phase in {"stopping", "stopped"}
            ):
                return

            failed = current
            failed["status"] = "failed"
            failed["phase"] = "failed"
            failed["failed_at"] = now_string()
            failed["message"] = "Backup script failed"
            failed["error"] = result.stderr[-1000:]
            write_status(failed)

    except Exception as e:
        current = read_status()
        current_status = str(current.get("status", "")).lower()
        current_phase = str(current.get("phase", "")).lower()

        if (
            current_status in {"stopping", "stopped"}
            or current_phase in {"stopping", "stopped"}
        ):
            return

        failed = current
        failed["status"] = "failed"
        failed["phase"] = "failed"
        failed["failed_at"] = now_string()
        failed["message"] = str(e)
        write_status(failed)

    finally:
        remove_lock()


@app.route("/api/backup")
def backup_status():
    try:
        data = read_status()
        data["running"] = backup_is_running()
        return jsonify(data)
    except Exception as e:
        return jsonify({
            "status": "error",
            "phase": "failed",
            "message": str(e),
            "running": False,
        }), 500


@app.route("/api/backup/run", methods=["POST"])
def run_backup():
    if backup_is_running() or not acquire_lock():
        return jsonify({
            "status": "already_running",
            "phase": "backup",
            "message": "Backup is already running",
            "running": True,
        }), 409

    thread = threading.Thread(target=run_backup_background, daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "phase": "backup",
        "message": "Backup started",
        "running": True,
    })


@app.route("/api/backup/stop", methods=["POST"])
def stop_backup():
    if not backup_is_running():
        data = read_status()
        data["running"] = False
        data["message"] = "No backup is currently running"
        return jsonify(data)

    current = read_status()
    current["status"] = "stopping"
    current["phase"] = "stopping"
    current["message"] = "Stopping backup"
    current["updated_at"] = now_string()
    write_status(current)

    result = run_ssh_command(STOP_COMMAND)

    if result.returncode != 0:
        failed = read_status()
        failed["status"] = "failed"
        failed["phase"] = "failed"
        failed["message"] = "Stop backup command failed"
        failed["failed_at"] = now_string()
        failed["error"] = result.stderr[-1000:]
        write_status(failed)

        return jsonify({
            "status": "failed",
            "phase": "failed",
            "message": "Stop backup command failed",
            "error": result.stderr[-1000:],
            "running": backup_is_running(),
        }), 500

    data = read_status()
    data["running"] = backup_is_running()
    return jsonify(data)


@app.route("/")
def index():
    return redirect("/backup-widget")


@app.route("/backup-widget")
def backup_widget():
    theme = request.args.get("theme", WIDGET_THEME).strip().lower()
    if theme not in VALID_WIDGET_THEMES:
        theme = WIDGET_THEME

    html = r"""
<!doctype html>
<html data-widget-theme="__WIDGET_THEME__">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">

  <style>
    :root {
      font-size: 10px;

      /*
       * "custom" preserves the original purple Glance widget appearance.
       * default-dark follows Glance's built-in dark color model.
       * default-light is the corresponding light presentation.
       * auto follows the browser/OS prefers-color-scheme setting.
       */
      --primary: #bb86fc;
      --backup: #ff5c8a;
      --uploading: #ffd166;
      --failed: #ff4d4d;

      --card-bg: #19191f;
      --card-border: #212026;
      --text: #f8f8f2;
      --text-subdue: #c9c9d3;
    }

    :root[data-widget-theme="default-dark"] {
      --primary: hsl(43, 50%, 70%);
      --backup: hsl(43, 50%, 70%);
      --uploading: hsl(43, 70%, 62%);
      --failed: hsl(0, 70%, 70%);

      --card-bg: hsl(240, 8%, 10%);
      --card-border: hsl(240, 8%, 13%);
      --text: hsl(240, 8%, 85%);
      --text-subdue: hsl(240, 8%, 58%);
    }

    :root[data-widget-theme="default-light"] {
      --primary: hsl(43, 55%, 32%);
      --backup: hsl(43, 55%, 32%);
      --uploading: hsl(35, 80%, 38%);
      --failed: hsl(0, 70%, 45%);

      --card-bg: hsl(0, 0%, 96%);
      --card-border: hsl(0, 0%, 91%);
      --text: hsl(0, 0%, 15%);
      --text-subdue: hsl(0, 0%, 42%);
    }

    @media (prefers-color-scheme: dark) {
      :root[data-widget-theme="auto"] {
        --primary: hsl(43, 50%, 70%);
        --backup: hsl(43, 50%, 70%);
        --uploading: hsl(43, 70%, 62%);
        --failed: hsl(0, 70%, 70%);

        --card-bg: hsl(240, 8%, 10%);
        --card-border: hsl(240, 8%, 13%);
        --text: hsl(240, 8%, 85%);
        --text-subdue: hsl(240, 8%, 58%);
      }
    }

    @media (prefers-color-scheme: light) {
      :root[data-widget-theme="auto"] {
        --primary: hsl(43, 55%, 32%);
        --backup: hsl(43, 55%, 32%);
        --uploading: hsl(35, 80%, 38%);
        --failed: hsl(0, 70%, 45%);

        --card-bg: hsl(0, 0%, 96%);
        --card-border: hsl(0, 0%, 91%);
        --text: hsl(0, 0%, 15%);
        --text-subdue: hsl(0, 0%, 42%);
      }
    }

    * {
      box-sizing: border-box;
    }

    html,
    body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: var(--card-bg);
      color: var(--text);
      font-family: "JetBrains Mono", monospace;
      font-size: 1.3rem;
      font-weight: 400;
      font-variant-ligatures: none;
      line-height: 1.6;
      overflow: hidden;
      -webkit-font-smoothing: antialiased;
      text-rendering: geometricPrecision;
    }

    .widget {
      width: 100%;
      height: 100%;
      padding: 18px 20px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 6px;

      display: grid;
      grid-template-columns: minmax(0, 1fr) max-content;
      grid-template-rows: auto auto;
      align-content: space-between;
      align-items: center;
      column-gap: 14px;
    }

    .title {
      align-self: center;
      color: var(--text);
      font-size: 1.2rem;
      font-weight: 400;
      line-height: 1.6;
      text-transform: uppercase;
      letter-spacing: 0;
    }

    .backup-times {
      min-width: 0;
      align-self: end;
      line-height: 1.35;
      white-space: nowrap;
      color: var(--text-subdue);
      font-size: 12px;
      font-weight: 400;
    }

    .relative-time,
    .last-backup {
      color: var(--text-subdue);
      font-size: 12px;
      font-weight: 400;
    }

    .right-status {
      justify-self: end;
      align-self: end;
      text-align: right;
      white-space: nowrap;
      line-height: 1.35;
      color: var(--text-subdue);
      font-size: 12px;
      font-weight: 400;
    }

    .button-row {
      justify-self: end;
      align-self: center;
      display: flex;
      gap: 8px;
      align-items: center;
    }

    button {
      padding: 6px 12px;
      min-width: 105px;
      border-radius: 8px;
      border: 1px solid var(--primary);
      background: transparent;
      color: var(--primary);
      cursor: pointer;
      white-space: nowrap;
      font-family: "JetBrains Mono", monospace;
      font-size: 1.2rem;
      font-weight: 400;
      font-variant-ligatures: none;
      line-height: 1.3;
    }

    #runButton {
      min-width: 105px;
    }

    #stopButton {
      min-width: 34px;
      width: 34px;
      padding: 6px 0;
      display: none;
      border-color: var(--failed);
      color: var(--failed);
    }

    button.backup {
      border-color: var(--backup);
      color: var(--backup);
    }

    button.uploading {
      border-color: var(--uploading);
      color: var(--uploading);
    }

    button.failed {
      border-color: var(--failed);
      color: var(--failed);
    }

    button:disabled {
      cursor: not-allowed;
      opacity: 0.95;
    }
  </style>
</head>

<body>
  <div class="widget">
    <div class="title">Last Backup</div>

    <div class="button-row">
      <button id="runButton" type="button">Run Backup</button>
      <button id="stopButton" type="button" title="Stop Backup">■</button>
    </div>

    <div class="backup-times">
      <div id="relativeTime" class="relative-time">Loading...</div>
      <div id="lastBackup" class="last-backup">Loading...</div>
    </div>

    <div class="right-status">
      <div id="size">...</div>
      <div id="status">...</div>
    </div>
  </div>

  <script>
    const runButton = document.getElementById("runButton");
    const stopButton = document.getElementById("stopButton");
    const relativeTime = document.getElementById("relativeTime");
    const lastBackup = document.getElementById("lastBackup");
    const size = document.getElementById("size");
    const statusText = document.getElementById("status");

    function relativeTimeText(timestamp) {
      if (!timestamp || timestamp === "Never" || timestamp === "Unknown") {
        return "Never";
      }

      const parsed = timestamp.replace(" ", "T");
      const then = new Date(parsed);
      const now = new Date();

      if (Number.isNaN(then.getTime())) {
        return "Unknown";
      }

      const diffSeconds = Math.max(0, Math.floor((now - then) / 1000));

      if (diffSeconds < 60) {
        return "just now";
      }

      const diffMinutes = Math.floor(diffSeconds / 60);
      if (diffMinutes < 60) {
        return diffMinutes === 1 ? "1 minute ago" : `${diffMinutes} minutes ago`;
      }

      const diffHours = Math.floor(diffMinutes / 60);
      if (diffHours < 24) {
        return diffHours === 1 ? "1 hour ago" : `${diffHours} hours ago`;
      }

      const diffDays = Math.floor(diffHours / 24);
      if (diffDays < 30) {
        return diffDays === 1 ? "1 day ago" : `${diffDays} days ago`;
      }

      const diffMonths = Math.floor(diffDays / 30);
      if (diffMonths < 12) {
        return diffMonths === 1 ? "1 month ago" : `${diffMonths} months ago`;
      }

      const diffYears = Math.floor(diffDays / 365);
      return diffYears === 1 ? "1 year ago" : `${diffYears} years ago`;
    }

    function runButtonTextForPhase(phase) {
      if (phase === "backup") return "Backing Up";
      if (phase === "uploading") return "Uploading";
      if (phase === "stopping") return "Stopping";
      if (phase === "failed") return "Failed";
      return "Run Backup";
    }

    function statusLabel(value) {
      if (!value) return "Unknown";

      return String(value)
        .replace(/_/g, " ")
        .split(" ")
        .filter(Boolean)
        .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
        .join(" ");
    }

    function applyButtonStyle(phase, running) {
      runButton.classList.remove("backup", "uploading", "failed");

      if (phase === "backup") {
        runButton.classList.add("backup");
      } else if (phase === "uploading") {
        runButton.classList.add("uploading");
      } else if (phase === "failed" || phase === "stopping") {
        runButton.classList.add("failed");
      }

      runButton.textContent = runButtonTextForPhase(phase);

      if (running || phase === "backup" || phase === "uploading" || phase === "stopping") {
        runButton.disabled = true;
        stopButton.style.display = "inline-block";
        stopButton.disabled = phase === "stopping";
      } else {
        runButton.disabled = false;
        stopButton.style.display = "none";
        stopButton.disabled = false;
        stopButton.textContent = "■";
      }
    }

    async function refreshStatus() {
      try {
        const response = await fetch("/api/backup", {
          cache: "no-store"
        });

        const data = await response.json();
        const phase = data.phase || "unknown";
        const running = Boolean(data.running);

        const backupTimestamp = data.last_backup || "Unknown";
        relativeTime.textContent = relativeTimeText(backupTimestamp);
        lastBackup.textContent = backupTimestamp;
        size.textContent = data.size || "Unknown";
        statusText.textContent = statusLabel(data.status);

        applyButtonStyle(phase, running);
      } catch (error) {
        relativeTime.textContent = "API error";
        lastBackup.textContent = "API error";
        size.textContent = "Unknown";
        statusText.textContent = "Failed";
        applyButtonStyle("failed", false);
      }
    }

    async function runBackup() {
      runButton.disabled = true;
      runButton.textContent = "Starting";

      try {
        const response = await fetch("/api/backup/run", {
          method: "POST",
          cache: "no-store"
        });

        if (!response.ok && response.status !== 409) {
          applyButtonStyle("failed", false);
          return;
        }
      } catch (error) {
        applyButtonStyle("failed", false);
        return;
      }

      await refreshStatus();
    }

    async function stopBackup() {
      stopButton.disabled = true;
      stopButton.textContent = "■";
      runButton.textContent = "Stopping";

      try {
        await fetch("/api/backup/stop", {
          method: "POST",
          cache: "no-store"
        });
      } catch (error) {
        applyButtonStyle("failed", false);
        return;
      }

      stopButton.textContent = "■";
      await refreshStatus();
    }

    runButton.addEventListener("click", runBackup);
    stopButton.addEventListener("click", stopBackup);

    refreshStatus();
    setInterval(refreshStatus, 3000);
  </script>
</body>
</html>
"""
    html = html.replace("__WIDGET_THEME__", theme)
    return Response(html, mimetype="text/html")


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3030)
