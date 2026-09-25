# Glance Backup Status

A small Dockerized backup API + iframe widget for Glance.

It can show backup status, start/stop backups, create local `.tar.gz` archives, optionally upload with `rclone`, and use `default-dark`, `default-light`, `custom`, or `auto` themes.

> The API has no built-in login. Keep it on a trusted LAN or Tailscale. Do not expose it directly to the public internet.

## Requirements

Linux server with Docker + Docker Compose, OpenSSH Server, Python 3, and `sudo`. `rclone` and Glance are optional.

## 1. Clone the repo

```bash
git clone YOUR_REPO_URL glance-backup-status
cd glance-backup-status
```

## 2. Create the SSH key

The container SSHes back into the Docker host to run the included scripts.

```bash
mkdir -p ssh data
chmod 700 ssh

ssh-keygen -t ed25519 \
  -f ./ssh/id_ed25519 \
  -C "glance-backup-status" \
  -N ""
```

Authorize the public key:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

grep -qxF "$(cat ./ssh/id_ed25519.pub)" ~/.ssh/authorized_keys \
  || cat ./ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
```

Never commit or share `ssh/id_ed25519`.

## 3. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Find your username and repo path:

```bash
whoami
pwd
```

Replace every `youruser` placeholder with your real username.

Example:

```dotenv
SSH_USER=youruser
SSH_HOST=host.docker.internal
HOST_PROJECT_DIR=/home/youruser/glance-backup-status

BACKUP_USER=youruser
BACKUP_ROOT=/home/youruser/backups/server-config
APPDATA=/home/youruser/appdata
MEDIA_ROOT=

BACKUP_USER_SSH=false
BACKUP_USER_CONFIG=false

RCLONE_ENABLED=false
RCLONE_REMOTE=gdrive
RCLONE_DESTINATION="Backups/server-config"

WIDGET_BIND=0.0.0.0
WIDGET_PORT=3030
WIDGET_THEME=default-dark
```

Important:

- `HOST_PROJECT_DIR` must be the absolute path to this repo.
- `BACKUP_ROOT` must not be inside `APPDATA`.
- Leave `RCLONE_ENABLED=false` for the first test.
- If you change `WIDGET_PORT`, use that port in the URLs below.

Theme values: `default-dark`, `default-light`, `custom`, `auto`.

## 4. Add the sudoers rule

```bash
sudo visudo -f /etc/sudoers.d/glance-backup-status
```

Add one line, replacing the username and paths:

```sudoers
youruser ALL=(root) NOPASSWD: /bin/bash /home/youruser/glance-backup-status/backup-script.sh, /bin/bash /home/youruser/glance-backup-status/stop-backup.sh
```

Check it:

```bash
sudo visudo -c
```

## 5. Start the container

```bash
docker compose up -d --build
```

Check it:

```bash
docker compose ps
docker compose logs --tail=100
```

## 6. Test the API

If `WIDGET_PORT=3030`:

```bash
curl http://127.0.0.1:3030/health
curl http://127.0.0.1:3030/api/backup
curl -X POST http://127.0.0.1:3030/api/backup/run
```

Watch status:

```bash
watch -n 2 'curl -s http://127.0.0.1:3030/api/backup'
```

Stop a running backup:

```bash
curl -X POST http://127.0.0.1:3030/api/backup/stop
```

## 7. Open the widget

```text
http://SERVER_IP:3030/
```

`/` redirects to `/backup-widget`.

Preview themes:

```text
http://SERVER_IP:3030/backup-widget?theme=default-dark
http://SERVER_IP:3030/backup-widget?theme=default-light
http://SERVER_IP:3030/backup-widget?theme=custom
http://SERVER_IP:3030/backup-widget?theme=auto
```

## 8. Optional: enable rclone

Configure `rclone` on the host, then edit `.env`:

```dotenv
RCLONE_ENABLED=true
RCLONE_REMOTE=gdrive
RCLONE_DESTINATION="Backups/server-config"
```

Restart:

```bash
docker compose up -d
```

Status messages use generic `rclone` wording, so the remote does not need to be Google Drive.

## 9. Add to Glance

```yaml
- type: iframe
  hide-header: true
  source: http://SERVER_IP:3030/backup-widget
  height: 125
```

If you changed `WIDGET_PORT`, use that port instead. The iframe URL must be reachable from the browser viewing Glance.

## Useful commands

```bash
docker compose up -d --build
docker compose logs -f
curl -s http://127.0.0.1:3030/api/backup
docker compose down
```

## Security notes

- Never commit `ssh/id_ed25519` or `.env`.
- Treat backup archives as sensitive.
- `BACKUP_USER_SSH=true` includes the user's `.ssh` directory.
- `BACKUP_USER_CONFIG=true` includes the user's `.config` directory.
- Prefer LAN/Tailscale access.
- Keep the sudoers rule limited to the two included scripts.
