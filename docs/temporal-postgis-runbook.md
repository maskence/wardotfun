# Temporal PostGIS rollout runbook

This rollout is intentionally gated. Merely setting a database URL starts
**shadow mode**; it does not switch API reads or the browser to vector tiles.

## 0. Upgrade the VPS first

Ubuntu 25.04 is end-of-life. Before PostGIS becomes production-critical:

1. Take a provider-level VPS snapshot and verify it is restorable.
2. Record `systemctl --failed`, enabled units, nginx configuration, firewall
   rules, Docker volumes, disk usage, and the current wardotfun revision.
3. Record a known-good response and screenshot for `tache.niglo.fun`; preserve
   its database/files and service definitions independently.
4. Upgrade 25.04 to 25.10, verify every existing service, then upgrade 25.10 to
   Ubuntu 26.04 LTS. The 26.04 release notes require 25.04 systems to pass
   through 24.04 LTS or 25.10; do not attempt a direct jump. Use the server
   release upgrader and keep console access available. See the
   [Ubuntu 26.04 upgrade paths](https://documentation.ubuntu.com/release-notes/26.04/)
   and [Ubuntu Server upgrade procedure](https://ubuntu.com/server/docs/how-to/software/upgrade-your-release/).
5. Re-run the recorded health checks for `tache.niglo.fun`, nginx, Docker, SSH,
   wardotfun, certificates, timers, and backups. Roll back the VPS snapshot if
   any pre-existing service cannot be restored promptly.

Do not provision the production database until this gate is signed off.

## 1. Provision PostGIS

The compose file pins the currently recommended stable legacy-volume variant,
PostgreSQL 17 with PostGIS 3.5. The image is based on the official PostgreSQL
image; its tag and volume path are documented by
[postgis/postgis](https://hub.docker.com/r/postgis/postgis/).

```bash
sudo install -d -m 0750 -o wardotfun -g wardotfun /etc/wardotfun /var/lib/wardotfun
sudo install -d -m 0700 -o wardotfun -g wardotfun /var/backups/wardotfun-postgres
sudo install -m 0600 -o wardotfun -g wardotfun deploy/postgis.env.example /etc/wardotfun/postgis.env
sudo install -m 0600 -o wardotfun -g wardotfun deploy/wardotfun.env.example /etc/wardotfun/wardotfun.env
```

Replace both `CHANGE_ME` values. URL-encode the password in
`WARDOTFUN_DATABASE_URL`. Keep both rollout flags at `0`.

```bash
docker compose --env-file /etc/wardotfun/postgis.env -f deploy/docker-compose.postgis.yml up -d
env/bin/pip install -r backend/requirements.txt
set -a
source /etc/wardotfun/wardotfun.env
set +a
env/bin/python -m backend.migrate
```

Confirm the container is healthy, PostGIS responds, the volume is named
`wardotfun_postgres`, and the port is bound only to `127.0.0.1`.

## 2. Import baselines atomically

Stop the old GeoConfirmed web synchronizer briefly so SQLite cannot change
during its copy. The mapper import treats each current pickle as the first
historical state; it does not invent pre-migration history.

```bash
env/bin/python -m backend.migrate_geolocations --sqlite backend/data/geoconfirmed.sqlite3
env/bin/python -m backend.ingestion_worker --baseline
```

The GeoConfirmed command compares every SQLite UUID and source hash before its
transaction commits. It also preserves icon metadata, evidence links,
geolocation proof links, gear, ORBAT, timestamps, coordinates, and the fixed
retention start.

## 3. Start seven-day shadow mode

Install and enable the units while both read flags remain `0`:

```bash
sudo install -m 0644 deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wardotfun-ingest.service
sudo systemctl enable --now wardotfun-shadow-verify.timer
sudo systemctl start wardotfun-backup.service
sudo systemctl enable --now wardotfun-backup.timer wardotfun-healthcheck.timer
```

The worker polls Google/KML sources every five minutes, ISW tactical layers
every five minutes, ISW control hourly, fortifications daily, and
GeoConfirmed every 30 minutes. It archives distinct raw bodies as deterministic
gzip files and inserts a snapshot only when normalized geometry/properties/style
changes. Advisory locks make duplicate workers safe.

For seven full days, require every line in
`/var/lib/wardotfun/shadow-comparisons.jsonl` to have `ok: true`. The verifier
compares hashes, feature counts, bounds, layer types, paint, and per-layer
counts. Investigate any mismatch; do not waive it without a replayable cause.

During shadow mode, exercise tiles directly even though the browser flag is off:

```bash
curl -fsS 'http://127.0.0.1:8000/api/map-state'
curl -fsS -o /tmp/test.pbf 'http://127.0.0.1:8000/api/map-tiles/SOURCE/SNAPSHOT/6/37/21.pbf'
```

Run the live acceptance suite against a disposable test database by setting
`WARDOTFUN_TEST_DATABASE_URL` and executing
`env/bin/python -m unittest tests.test_postgis_integration -v`.

## 4. Backups, restore drill, and alerts

Install the PostgreSQL 17 client so `pg_dump` and `pg_restore` match the server.
The nightly unit writes verified custom-format dumps and retains seven daily
and four weekly generations. Copy backups off the VPS; local-only backups do
not protect against disk or provider loss.

Before cutover, restore a dump into a separate database and verify counts,
snapshot hashes, a current tile, a historical tile, and a GeoConfirmed detail.
The health timer exits nonzero and optionally POSTs to
`WARDOTFUN_ALERT_WEBHOOK` for:

- a failed or overdue ingestion;
- a database error;
- a missing/overdue backup marker;
- disk usage at or above 70%.

Also route failed systemd units to the VPS's normal alerting channel.

## 5. Nginx and cutover

Merge `deploy/nginx-temporal.conf` into the existing nginx `http` and server
blocks. Test with `nginx -t`, reload, and verify gzip/ETag behavior. Snapshot
URLs are immutable and can be cached for one year; manifests remain
revalidated.

Create the observation timeline and resumably backfill every stored transition
before enabling the drawer:

```bash
sudo -u wardotfun /opt/wardotfun/venv/bin/python -m backend.migrate --backfill-map-changes
```

Re-running the command is safe; sources with an observation timeline are
skipped. The first snapshot is a baseline and does not create a feed card.

Set the two flags in `/etc/wardotfun/wardotfun.env`:

```text
WARDOTFUN_POSTGIS_READS_ENABLED=1
WARDOTFUN_VECTOR_TILES_ENABLED=1
WARDOTFUN_MAP_CHANGES_ENABLED=1
```

Restart the wardotfun web service. Verify:

- map-state p95 below 300 ms;
- uncached tile p95 below 500 ms and cached tile p95 below 100 ms;
- usable visible geometry within three seconds on normal broadband;
- current and historical dates resolve the expected mapper, fortification, and
  GeoConfirmed states;
- switching dates/mappers requests `.pbf` viewport tiles and never downloads a
  whole-country overlay;
- market, city, and GeoConfirmed markers stay above geometry;
- upstream blocking leaves the latest snapshot available with stale status;
- nginx/browser caches serve repeated views.
- map-change cards follow the global calendar, and Before/Changes/After uses
  exact immutable snapshots without downloading whole-country GeoJSON;

Rollback is immediate: set all three flags back to `0` and restart the web service.
The ingestion worker continues maintaining legacy caches during the observation
week.

## 6. Retire legacy storage

After at least seven stable production days, make a separate reviewed change to
remove `/api/mapper-overlay`, `/api/fortifications`, frontend GeoJSON fallback,
pickle writes, and tracked `backend/data/mapper_cache/*.pkl`. Preserve the raw
archive and all PostGIS snapshots indefinitely. Keep a tested database restore
procedure and continue nightly/offsite backups.

## Persistent local ingestion

The web server and ingestion worker are intentionally separate. For a local
checkout at `~/code/wardotfun`, install the user unit once:

```bash
cp deploy/systemd/wardotfun-ingest-local.service ~/.config/systemd/user/wardotfun-ingest.service
systemctl --user daemon-reload
systemctl --user enable --now wardotfun-ingest.service
loginctl enable-linger "$USER"
```

The unit loads the repository `.env`, starts with the user service manager,
and restarts after failures. User lingering keeps it running after logout and
starts it after boot. Inspect it with:

```bash
systemctl --user status wardotfun-ingest.service
journalctl --user -u wardotfun-ingest.service -f
```
