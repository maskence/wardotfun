#!/usr/bin/env bash
set -euo pipefail

backup_root="${WARDOTFUN_BACKUP_DIR:-/var/backups/wardotfun-postgres}"
database_url="${WARDOTFUN_DATABASE_URL:?WARDOTFUN_DATABASE_URL is required}"

case "$backup_root" in
  ""|/|/var|/var/backups) echo "Refusing unsafe backup root: $backup_root" >&2; exit 2 ;;
esac

daily_dir="$backup_root/daily"
weekly_dir="$backup_root/weekly"
mkdir -p "$daily_dir" "$weekly_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
final="$daily_dir/wardotfun-$stamp.dump"
temporary="$daily_dir/.wardotfun-$stamp.dump.tmp"
trap 'test ! -f "$temporary" || unlink "$temporary"' EXIT

pg_dump "$database_url" --format=custom --compress=9 --file="$temporary"
pg_restore --list "$temporary" >/dev/null
chmod 0600 "$temporary"
mv "$temporary" "$final"

if [ "$(date -u +%u)" = "7" ]; then
  cp --reflink=auto "$final" "$weekly_dir/wardotfun-$stamp.dump"
fi

find "$daily_dir" -type f -name 'wardotfun-*.dump' -mtime +6 -delete
find "$weekly_dir" -type f -name 'wardotfun-*.dump' -mtime +27 -delete
touch "$backup_root/.last_success"
