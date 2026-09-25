#!/usr/bin/env bash
set -euo pipefail

application_root=/home/ubuntu/wardotfun
environment_file=/etc/wardotfun/wardotfun.env
virtualenv_bin="$application_root/.venv/bin"

exec 9>/tmp/wardotfun-production-deploy.lock
flock 9

cd "$application_root"

"$virtualenv_bin/pip" install --disable-pip-version-check -r backend/requirements.txt

set -a
# shellcheck disable=SC1090
. "$environment_file"
set +a

change_thumbnail_dir="${WARDOTFUN_CHANGE_THUMBNAIL_DIR:-/var/lib/wardotfun/change-thumbnails}"
sudo install -d -m 0750 -o ubuntu -g ubuntu "$change_thumbnail_dir"

"$virtualenv_bin/python" -m backend.migrate

sudo install -m 0644 deploy/systemd/wardotfun-ingest.service /etc/systemd/system/wardotfun-ingest.service
sudo install -m 0644 deploy/systemd/wardotfun-backup.service /etc/systemd/system/wardotfun-backup.service
sudo install -m 0644 deploy/systemd/wardotfun-backup.timer /etc/systemd/system/wardotfun-backup.timer
sudo install -m 0644 deploy/systemd/wardotfun-healthcheck.service /etc/systemd/system/wardotfun-healthcheck.service
sudo install -m 0644 deploy/systemd/wardotfun-healthcheck.timer /etc/systemd/system/wardotfun-healthcheck.timer
sudo install -d -m 0755 /etc/systemd/system/wardotfun.service.d
sudo install -m 0644 deploy/systemd/wardotfun.service.d/temporal.conf /etc/systemd/system/wardotfun.service.d/temporal.conf
sudo systemctl daemon-reload

if ! sudo test -f /var/backups/wardotfun-postgres/.last_success; then
    sudo systemctl start wardotfun-backup.service
fi

sudo systemctl enable --now wardotfun-backup.timer wardotfun-healthcheck.timer
sudo systemctl enable wardotfun-ingest.service
sudo systemctl restart wardotfun-ingest.service
sudo systemctl restart wardotfun.service
sudo systemctl is-active --quiet wardotfun-ingest.service

for attempt in $(seq 1 30); do
    if health_json="$(curl --fail --silent --show-error http://127.0.0.1:8000/health 2>/dev/null)"; then
        if HEALTH_JSON="$health_json" "$virtualenv_bin/python" - <<'PY'
import json
import os

health = json.loads(os.environ["HEALTH_JSON"])
# A recent upstream fetch failure makes the application "degraded" while it
# continues serving the last good immutable snapshots. That is a healthy
# deployment outcome; service/unit failures are checked separately above.
assert health.get("status") in {"ok", "degraded"}, health
assert health.get("storage") == "postgis", health
assert health.get("vector_tiles_enabled") is True, health
assert health.get("map_changes_enabled") is True, health
PY
        then
            printf '%s\n' "$health_json"
            exit 0
        fi
    fi
    sleep 2
done

echo "wardotfun failed its post-deployment health check" >&2
sudo systemctl --no-pager --full status wardotfun.service wardotfun-ingest.service >&2 || true
exit 1
