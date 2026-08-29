"""Operational checks suitable for a systemd timer and OnFailure alert."""
from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from .database import PostGISDatabase
except ImportError:
    from database import PostGISDatabase


def _alert(payload):
    webhook = os.getenv("WARDOTFUN_ALERT_WEBHOOK")
    if not webhook:
        return
    body = json.dumps(
        {"text": "wardotfun temporal health failure", "details": payload},
        default=str,
    ).encode()
    request = urllib.request.Request(
        webhook, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=15):
        pass


def run_checks(database=None, now=None):
    database = database or PostGISDatabase()
    now = now or datetime.now(timezone.utc)
    failures = []
    details = {}
    try:
        with database.connect(dict_rows=True) as conn:
            latest = conn.execute(
                """
                SELECT s.id, s.refresh_policy, run.status, run.finished_at, run.error
                FROM map_sources s
                LEFT JOIN LATERAL (
                    SELECT status, finished_at, error FROM ingest_runs
                    WHERE source_id = s.id ORDER BY started_at DESC LIMIT 1
                ) run ON true WHERE s.enabled ORDER BY s.id
                """
            ).fetchall()
            details["sources"] = latest
            for row in latest:
                if row["status"] == "failed":
                    failures.append(f"ingestion failed for {row['id']}: {row['error'] or 'unknown error'}")
                policy = row["refresh_policy"] or {}
                interval = max(
                    [float(policy.get("default_seconds") or 300)]
                    + [float(value) for value in (policy.get("layers") or {}).values()]
                )
                allowed = max(interval * 3, 20 * 60)
                if not row["finished_at"] or (now - row["finished_at"]).total_seconds() > allowed:
                    failures.append(f"ingestion overdue for {row['id']}")
            geo_meta = {
                row["key"]: row["value"]
                for row in conn.execute(
                    "SELECT key, value FROM geolocation_metadata WHERE key IN ('last_sync', 'last_error')"
                )
            }
            details["geoconfirmed"] = geo_meta
            if geo_meta.get("last_error"):
                failures.append(f"GeoConfirmed ingestion failed: {geo_meta['last_error']}")
            last_geo_sync = float(geo_meta.get("last_sync") or 0)
            if not last_geo_sync or now.timestamp() - last_geo_sync > 2 * 60 * 60:
                failures.append("GeoConfirmed ingestion is overdue")
    except Exception as exc:
        failures.append(f"database error: {exc}")

    disk_path = Path(os.getenv("WARDOTFUN_DISK_PATH", "/var/lib/docker"))
    try:
        usage = shutil.disk_usage(disk_path)
        percent = usage.used * 100 / usage.total
        details["disk_percent"] = round(percent, 2)
        if percent >= float(os.getenv("WARDOTFUN_DISK_ALERT_PERCENT", "70")):
            failures.append(f"disk usage is {percent:.1f}% at {disk_path}")
    except Exception as exc:
        failures.append(f"disk check error: {exc}")

    backup_root = Path(os.getenv("WARDOTFUN_BACKUP_DIR", "/var/backups/wardotfun-postgres"))
    marker = backup_root / ".last_success"
    try:
        modified = datetime.fromtimestamp(marker.stat().st_mtime, timezone.utc)
        details["last_backup"] = modified.isoformat()
        if now - modified > timedelta(hours=30):
            failures.append("nightly database backup is overdue")
    except FileNotFoundError:
        failures.append("database backup success marker is missing")
    except Exception as exc:
        failures.append(f"backup check error: {exc}")

    result = {"ok": not failures, "checked_at": now.isoformat(), "failures": failures, **details}
    if failures:
        try:
            _alert(result)
        except Exception as exc:
            result["alert_error"] = str(exc)
    return result


def main():
    result = run_checks()
    print(json.dumps(result, default=str, sort_keys=True))
    if not result["ok"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
