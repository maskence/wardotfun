"""Persistent GeoConfirmed placemark mirror and query service."""
from __future__ import annotations

import csv, hashlib, io, json, logging, mimetypes, os, re, sqlite3, threading, time
import urllib.error, urllib.parse
from datetime import date, datetime, time as day_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "geoconfirmed.sqlite3"
ICON_DIR = DATA_DIR / "geoconfirmed_icons"
API_ROOT = "https://geoconfirmed.org"
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
URL_RE = re.compile(r"https?://[^\s,;<>\"']+", re.I)

# Rough mainland/Crimea extent, expanded by 5% of its width and height on every
# side so near-border activity remains visible.
UKRAINE_NEAR_BOUNDS = (43.575, 52.925, 21.075, 41.425)  # south, north, west, east
KYIV_TZ = ZoneInfo("Europe/Kyiv")


def kyiv_today():
    return datetime.now(KYIV_TZ).date()


def is_near_ukraine(lat, lon):
    south, north, west, east = UKRAINE_NEAR_BOUNDS
    try:
        return south <= float(lat) <= north and west <= float(lon) <= east
    except (TypeError, ValueError):
        return False


def extract_urls(value):
    seen, result = set(), []
    for found in URL_RE.findall(str(value or "")):
        url = found.rstrip(".)],;:!?")
        if url and url not in seen:
            seen.add(url); result.append(url)
    return result


def parse_timestamp(value):
    if not value: return None
    value = str(value).strip().replace("Z", "+00:00")
    try: return datetime.fromisoformat(value)
    except ValueError:
        try: return datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError: return None


def timestamp_precision(value):
    parsed = parse_timestamp(value)
    return "minute" if parsed and parsed.time() != day_time.min else "day"


def parse_csv_export(payload):
    text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload.lstrip("\ufeff")
    return [dict(row) for row in csv.DictReader(io.StringIO(text, newline=""), delimiter=";")]


def compact_index(payload):
    events, icons = {}, {}
    for faction in payload if isinstance(payload, list) else []:
        fm = {"faction_id": str(faction.get("factionId") or ""), "faction_name": faction.get("name") or "Unknown", "faction_color": faction.get("color") or "#666666"}
        for group in faction.get("icons") or []:
            iid, path = str(group.get("iconId") or ""), group.get("icon") or ""
            if iid: icons[iid] = {**fm, "icon_id": iid, "icon_path": path, "icon_name": group.get("name")}
            for place in group.get("placemarks") or []:
                uid = str(place.get("id") or "")
                if uid: events[uid] = {**fm, "icon_id": str(place.get("iconId") or iid), "icon_path": place.get("icon") or path, "timestamp": place.get("date"), "lat": place.get("la"), "lon": place.get("lo")}
    return events, icons


def split_names(value):
    return list(dict.fromkeys(part.strip() for part in re.split(r"[\n,;]+", str(value or "")) if part.strip()))


class GeoConfirmedClient:
    def __init__(self, user_agent=None, retries=4):
        self.user_agent = user_agent or os.getenv("GEOCONFIRMED_USER_AGENT", "wardotfun/1.0 (+https://war.fun)")
        self.retries = retries

    def get_bytes(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        url = f"{API_ROOT}{path}{'?' + query if query else ''}"
        for attempt in range(self.retries):
            try:
                req = Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
                with urlopen(req, timeout=60) as response:
                    return response.read(), response.headers.get_content_type()
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and exc.code < 500: raise
                retry = exc.headers.get("Retry-After")
                try: delay = max(0.0, float(retry))
                except (TypeError, ValueError):
                    try: delay = max(0.0, (parsedate_to_datetime(retry) - datetime.now().astimezone()).total_seconds())
                    except (TypeError, ValueError): delay = 2 ** attempt
            except (urllib.error.URLError, TimeoutError):
                if attempt + 1 >= self.retries: raise
                delay = 2 ** attempt
            if attempt + 1 >= self.retries: raise
            time.sleep(delay)
        raise RuntimeError("GeoConfirmed retries exhausted")

    def get_json(self, path): return json.loads(self.get_bytes(path)[0])
    def get_csv(self, start, end):
        body, _ = self.get_bytes("/api/Map/export/Ukraine/csv", {"start": f"{start.isoformat()}T00:00:00Z", "end": f"{end.isoformat()}T00:00:00Z"})
        return parse_csv_export(body)


class GeoConfirmedGeolocationsSource:
    id, display_name = "geoconfirmed", "GeoConfirmed"
    refresh_interval, reconciliation_interval = 1800, 7 * 86400

    def __init__(self, db_path=DB_PATH, icon_dir=ICON_DIR, client=None, today=None):
        self.db_path, self.icon_dir, self.client, self.today = Path(db_path), Path(icon_dir), client or GeoConfirmedClient(), today or kyiv_today
        self._sync_lock, self._last_error = threading.Lock(), None
        self._setup()

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30); db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL"); return db

    def _setup(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True); self.icon_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS icons(id TEXT PRIMARY KEY,name TEXT,upstream_path TEXT,local_name TEXT,content_type TEXT,updated_at REAL);
            CREATE TABLE IF NOT EXISTS events(uuid TEXT PRIMARY KEY,event_date TEXT NOT NULL,timestamp TEXT NOT NULL,time_precision TEXT NOT NULL,lat REAL NOT NULL,lon REAL NOT NULL,description TEXT NOT NULL DEFAULT '',faction_id TEXT NOT NULL DEFAULT '',faction_name TEXT NOT NULL DEFAULT 'Unknown',faction_color TEXT NOT NULL DEFAULT '#666666',icon_id TEXT,icon_name TEXT,icon_path TEXT,origin TEXT NOT NULL DEFAULT '',equipment TEXT NOT NULL DEFAULT '',units TEXT NOT NULL DEFAULT '',plus_code TEXT NOT NULL DEFAULT '',evidence_links TEXT NOT NULL DEFAULT '[]',geolocation_links TEXT NOT NULL DEFAULT '[]',gear_items TEXT NOT NULL DEFAULT '[]',orbat_units TEXT NOT NULL DEFAULT '[]',source_hash TEXT NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_geo_date ON events(event_date); CREATE INDEX IF NOT EXISTS idx_geo_faction ON events(faction_id); CREATE INDEX IF NOT EXISTS idx_geo_icon ON events(icon_id);
            """)
            if self._meta(db, "retention_start") is None: self._set_meta(db, "retention_start", (self.today() - timedelta(days=90)).isoformat())

    @staticmethod
    def _meta(db, key):
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone(); return row[0] if row else None
    @staticmethod
    def _set_meta(db, key, value): db.execute("INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    @property
    def retention_start(self):
        with self._db() as db: return date.fromisoformat(self._meta(db, "retention_start"))

    def load_cache(self):
        with self._db() as db: count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        log.info("Loaded GeoConfirmed cache: %d events", count)

    def refresh_if_due(self):
        with self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            last, weekly = float(self._meta(db, "last_sync") or 0), float(self._meta(db, "last_reconcile") or 0)
        now = time.time()
        if not count: self.initial_import()
        elif now - weekly >= self.reconciliation_interval: self.reconcile()
        elif now - last >= self.refresh_interval: self.incremental_sync()

    def initial_import(self): return self._full_sync(False)
    def reconcile(self): return self._full_sync(True)

    def _full_sync(self, reconcile):
        if not self._sync_lock.acquire(False): return False
        try:
            rows = self.client.get_csv(self.retention_start, self.today() + timedelta(days=1))
            index, basic_icons = compact_index(self.client.get_json("/api/Placemark/Ukraine"))
            _, named_icons = compact_index(self.client.get_json("/api/Placemark/Ukraine/icons"))
            icons = {**basic_icons, **named_icons}; records = self._merge_csv(rows, index, icons)
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE"); self._store_icons(db, icons); self._upsert(db, records)
                if reconcile:
                    retained = [item["uuid"] for item in records]
                    if retained:
                        marks = ",".join("?" for _ in retained); db.execute(f"DELETE FROM events WHERE event_date>=? AND uuid NOT IN ({marks})", (self.retention_start.isoformat(), *retained))
                    else: db.execute("DELETE FROM events WHERE event_date>=?", (self.retention_start.isoformat(),))
                now = time.time(); self._set_meta(db, "last_sync", now); self._set_meta(db, "last_reconcile", now); db.commit()
            self._cache_icons(); self._last_error = None; return True
        except Exception as exc:
            self._last_error = str(exc); log.warning("GeoConfirmed full sync failed; serving stale data: %s", exc); return False
        finally: self._sync_lock.release()

    def incremental_sync(self):
        if not self._sync_lock.acquire(False): return False
        try:
            start = max(self.retention_start, self.today() - timedelta(days=7))
            records = self._merge_csv(self.client.get_csv(start, self.today() + timedelta(days=1)), {}, {})
            with self._db() as db:
                hashes = {r["uuid"]: r["source_hash"] for r in db.execute("SELECT uuid,source_hash FROM events WHERE event_date>=?", (start.isoformat(),))}
                icon_rows = {r["upstream_path"]: dict(r) for r in db.execute("SELECT * FROM icons WHERE upstream_path IS NOT NULL")}
                factions = {r["faction_name"]: dict(r) for r in db.execute("SELECT faction_name,faction_id,faction_color FROM events GROUP BY faction_name")}
            changed = []
            for record in records:
                if hashes.get(record["uuid"]) != record["source_hash"]:
                    changed.append(self._merge_detail(record, self.client.get_json(f"/api/Placemark/detail/{record['uuid']}"), icon_rows, factions))
            with self._db() as db:
                db.execute("BEGIN IMMEDIATE"); self._upsert(db, changed); self._set_meta(db, "last_sync", time.time()); db.commit()
            self._cache_icons(); self._last_error = None; return True
        except Exception as exc:
            self._last_error = str(exc); log.warning("GeoConfirmed incremental sync failed; serving stale data: %s", exc); return False
        finally: self._sync_lock.release()

    def _merge_csv(self, rows, index, icons):
        records, tomorrow = [], self.today() + timedelta(days=1)
        for row in rows:
            uid = str(row.get("Id") or "").strip(); compact = index.get(uid, {}); stamp = compact.get("timestamp") or row.get("Date"); parsed = parse_timestamp(stamp)
            if not UUID_RE.fullmatch(uid) or not parsed or not (self.retention_start <= parsed.date() < tomorrow): continue
            iid = compact.get("icon_id") or None; icon = icons.get(iid, {})
            records.append({"uuid":uid,"event_date":parsed.date().isoformat(),"timestamp":stamp,"time_precision":timestamp_precision(stamp),"lat":float(compact.get("lat") if compact.get("lat") is not None else row["Latitude"]),"lon":float(compact.get("lon") if compact.get("lon") is not None else row["Longitude"]),"description":str(row.get("Description") or row.get("Name") or "").strip(),"faction_id":compact.get("faction_id") or icon.get("faction_id") or "","faction_name":compact.get("faction_name") or row.get("Faction") or "Unknown","faction_color":compact.get("faction_color") or icon.get("faction_color") or "#666666","icon_id":iid,"icon_name":icon.get("icon_name"),"icon_path":compact.get("icon_path") or icon.get("icon_path"),"origin":str(row.get("Origin") or "").strip(),"equipment":str(row.get("Equipment") or "").strip(),"units":str(row.get("Units") or "").strip(),"plus_code":str(row.get("PlusCode") or "").strip(),"evidence_links":extract_urls(row.get("Source")),"geolocation_links":extract_urls(row.get("Geolocation")),"gear_items":[{"name":n} for n in split_names(row.get("EquipmentItems"))],"orbat_units":[{"name":n} for n in split_names(row.get("OrbatUnits"))],"source_hash":hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),"updated_at":time.time()})
        return records

    @staticmethod
    def _merge_detail(record, payload, icons, factions):
        if not isinstance(payload, dict): return record
        path = payload.get("icon") or record["icon_path"]; icon = icons.get(path, {}); faction = factions.get(payload.get("faction"), {}); stamp = payload.get("date") or record["timestamp"]
        return {**record,"event_date":parse_timestamp(stamp).date().isoformat(),"timestamp":stamp,"time_precision":timestamp_precision(stamp),"lat":float(payload.get("latitude",record["lat"])),"lon":float(payload.get("longitude",record["lon"])),"description":str(payload.get("description") or record["description"]),"faction_id":faction.get("faction_id",record["faction_id"]),"faction_name":payload.get("faction") or record["faction_name"],"faction_color":faction.get("faction_color",record["faction_color"]),"icon_id":icon.get("id") or record["icon_id"],"icon_name":icon.get("name") or record["icon_name"],"icon_path":path,"origin":str(payload.get("origin") or record["origin"]),"equipment":str(payload.get("gear") or record["equipment"]),"units":str(payload.get("units") or record["units"]),"plus_code":str(payload.get("plusCode") or record["plus_code"]),"evidence_links":extract_urls(payload.get("originalSource")),"geolocation_links":extract_urls(payload.get("geolocation")),"gear_items":payload.get("gearItems") if isinstance(payload.get("gearItems"),list) else record["gear_items"],"orbat_units":payload.get("orbatUnits") if isinstance(payload.get("orbatUnits"),list) else record["orbat_units"],"updated_at":time.time()}

    @staticmethod
    def _store_icons(db, icons): db.executemany("INSERT INTO icons(id,name,upstream_path) VALUES(:icon_id,:icon_name,:icon_path) ON CONFLICT(id) DO UPDATE SET name=excluded.name,upstream_path=excluded.upstream_path", [i for i in icons.values() if i.get("icon_id")])
    @staticmethod
    def _upsert(db, records):
        cols = "uuid event_date timestamp time_precision lat lon description faction_id faction_name faction_color icon_id icon_name icon_path origin equipment units plus_code evidence_links geolocation_links gear_items orbat_units source_hash updated_at".split(); sql = f"INSERT INTO events({','.join(cols)}) VALUES({','.join(':'+c for c in cols)}) ON CONFLICT(uuid) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols[1:])
        items = []
        for record in records:
            item = dict(record)
            for key in ("evidence_links","geolocation_links","gear_items","orbat_units"): item[key] = json.dumps(item[key], ensure_ascii=False)
            items.append(item)
        db.executemany(sql, items)

    def _cache_icons(self):
        with self._db() as db: rows = db.execute("SELECT DISTINCT e.icon_id,e.icon_path,i.local_name FROM events e LEFT JOIN icons i ON i.id=e.icon_id WHERE e.icon_id IS NOT NULL AND e.icon_path IS NOT NULL").fetchall()
        for row in rows:
            if row["local_name"] and (self.icon_dir / row["local_name"]).is_file(): continue
            try:
                body, mime = self.client.get_bytes(row["icon_path"]); ext = mimetypes.guess_extension(mime or "") or Path(row["icon_path"]).suffix or ".png"; name = f"{row['icon_id']}{ext}"; tmp = self.icon_dir / f".{name}.tmp"; tmp.write_bytes(body); tmp.replace(self.icon_dir / name)
                with self._db() as db: db.execute("UPDATE icons SET local_name=?,content_type=?,updated_at=? WHERE id=?", (name,mime,time.time(),row["icon_id"]))
            except Exception as exc: log.warning("Could not cache GeoConfirmed icon %s: %s", row["icon_id"], exc)

    def get_icon(self, icon_id):
        if not UUID_RE.fullmatch(icon_id): return None
        with self._db() as db: row = db.execute("SELECT local_name,content_type FROM icons WHERE id=?", (icon_id,)).fetchone()
        if not row or not row["local_name"]: return None
        path = self.icon_dir / row["local_name"]; return (path, row["content_type"] or "image/png") if path.is_file() else None

    @staticmethod
    def _event(row, detail=False):
        event = {"uuid":row["uuid"],"lat":row["lat"],"lon":row["lon"],"description":row["description"],"event_date":row["event_date"],"timestamp":row["timestamp"],"time_precision":row["time_precision"],"faction_id":row["faction_id"],"faction_name":row["faction_name"],"faction_color":row["faction_color"],"icon_id":row["icon_id"],"icon_name":row["icon_name"],"icon_url":f"/api/geolocations/icons/{row['icon_id']}" if row["icon_id"] else None,"origin":row["origin"],"equipment":row["equipment"],"units":row["units"]}
        if detail: event.update({"evidence_links":json.loads(row["evidence_links"]),"geolocation_links":json.loads(row["geolocation_links"]),"plus_code":row["plus_code"],"gear_items":json.loads(row["gear_items"]),"orbat_units":json.loads(row["orbat_units"]),"attribution":{"name":"GeoConfirmed","url":"https://geoconfirmed.org/ukraine"}})
        return event

    def get_events_for_date(self, event_date, **filters):
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        clauses = ["event_date=?", "lat BETWEEN ? AND ?", "lon BETWEEN ? AND ?"]
        values = [event_date, south, north, west, east]
        for key,col in {"faction":"faction_id","icon":"icon_id","origin":"origin"}.items():
            if filters.get(key): clauses.append(f"{col}=?"); values.append(str(filters[key]))
        if filters.get("q"):
            clauses.append("(description LIKE ? ESCAPE '\\' OR equipment LIKE ? ESCAPE '\\' OR units LIKE ? ESCAPE '\\')"); value = str(filters["q"]).replace("\\","\\\\").replace("%","\\%").replace("_","\\_"); values += [f"%{value}%"]*3
        with self._db() as db: return [self._event(r) for r in db.execute(f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY timestamp DESC,uuid", values)]

    def get_detail(self, uuid):
        if not UUID_RE.fullmatch(uuid): return None
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        with self._db() as db:
            row = db.execute(
                "SELECT * FROM events WHERE uuid=? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (uuid, south, north, west, east),
            ).fetchone()
        return self._event(row, True) if row else None

    def get_all(self, selected_date=None, **filters):
        if selected_date is not None and not re.fullmatch(r"\d{8}", selected_date): raise ValueError("date must use YYYYMMDD format")
        dates, cursor = [], self.retention_start
        while cursor <= self.today(): dates.append(cursor.strftime("%Y%m%d")); cursor += timedelta(days=1)
        selected = selected_date or dates[-1]
        if selected not in dates: raise ValueError(f"geolocation date is not available: {selected}")
        events = self.get_events_for_date(datetime.strptime(selected,"%Y%m%d").date().isoformat(), **filters)
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        bounds = (south, north, west, east)
        with self._db() as db:
            counts = {
                r["day"]: r["count"]
                for r in db.execute(
                    "SELECT replace(event_date,'-','') day,COUNT(*) count FROM events "
                    "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ? GROUP BY event_date",
                    bounds,
                )
            }
            last = float(self._meta(db,"last_sync") or 0) or None
            total = db.execute(
                "SELECT COUNT(*) FROM events WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                bounds,
            ).fetchone()[0]
        factions=sorted({(e["faction_id"],e["faction_name"],e["faction_color"]) for e in events}); icons=sorted({(e["icon_id"],e["icon_name"] or "Uncategorized") for e in events if e["icon_id"]}); origins=sorted({e["origin"] for e in events if e["origin"]})
        return {"date":selected,"dates":dates,"daily_counts":{d:counts.get(d,0) for d in dates},"events":events,"filters":{"factions":[{"id":x[0],"name":x[1],"color":x[2]} for x in factions],"icons":[{"id":x[0],"name":x[1]} for x in icons],"origins":origins},"sources":[{"id":self.id,"display_name":self.display_name,"event_count":len(events),"retained_event_count":total,"last_updated":last,"status":"stale" if self._last_error and total else "error" if self._last_error else "ok" if total else "empty"}]}


class PostGISGeoConfirmedGeolocationsSource(GeoConfirmedGeolocationsSource):
    """GeoConfirmed storage compatible with the legacy service response shape."""

    def __init__(self, database=None, icon_dir=ICON_DIR, client=None, today=None, *, read_only=False):
        try:
            from .database import PostGISDatabase
        except ImportError:
            from database import PostGISDatabase

        self.database = database or PostGISDatabase()
        self.icon_dir, self.client, self.today = Path(icon_dir), client or GeoConfirmedClient(), today or kyiv_today
        self.read_only = read_only
        self._sync_lock, self._last_error = threading.Lock(), None
        self.icon_dir.mkdir(parents=True, exist_ok=True)
        if not read_only:
            with self.database.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO geolocation_metadata(key, value) VALUES ('retention_start', %s)
                    ON CONFLICT(key) DO NOTHING
                    """,
                    ((self.today() - timedelta(days=90)).isoformat(),),
                )

    @staticmethod
    def _meta_pg(conn, key):
        row = conn.execute(
            "SELECT value FROM geolocation_metadata WHERE key = %s", (key,)
        ).fetchone()
        if not row:
            return None
        return row["value"] if hasattr(row, "keys") else row[0]

    @staticmethod
    def _set_meta_pg(conn, key, value):
        conn.execute(
            """
            INSERT INTO geolocation_metadata(key, value) VALUES (%s, %s)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    @property
    def retention_start(self):
        with self.database.connect() as conn:
            value = self._meta_pg(conn, "retention_start")
        return date.fromisoformat(value) if value else self.today() - timedelta(days=90)

    def load_cache(self):
        with self.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM geolocation_events").fetchone()[0]
            self._last_error = self._meta_pg(conn, "last_error") or None
        log.info("Loaded PostGIS GeoConfirmed cache: %d events", count)

    def refresh_if_due(self):
        if self.read_only:
            return
        with self.database.connect() as conn:
            count = conn.execute("SELECT count(*) FROM geolocation_events").fetchone()[0]
            last = float(self._meta_pg(conn, "last_sync") or 0)
            weekly = float(self._meta_pg(conn, "last_reconcile") or 0)
        now = time.time()
        if not count:
            self.initial_import()
        elif now - weekly >= self.reconciliation_interval:
            self.reconcile()
        elif now - last >= self.refresh_interval:
            self.incremental_sync()

    def _try_ingest_lock(self):
        conn = self.database.connect(autocommit=True)
        acquired = conn.execute(
            "SELECT pg_try_advisory_lock(hashtextextended('wardotfun:ingest:geoconfirmed', 0))"
        ).fetchone()[0]
        if not acquired:
            conn.close()
            return None
        return conn

    @staticmethod
    def _release_ingest_lock(conn):
        if not conn:
            return
        try:
            conn.execute(
                "SELECT pg_advisory_unlock(hashtextextended('wardotfun:ingest:geoconfirmed', 0))"
            )
        finally:
            conn.close()

    def _record_sync_error(self, error):
        try:
            with self.database.connect() as conn:
                self._set_meta_pg(conn, "last_error", str(error)[:4000])
                self._set_meta_pg(conn, "last_error_at", time.time())
        except Exception:
            log.exception("Could not persist GeoConfirmed sync failure")

    @staticmethod
    def _json(value):
        return value if isinstance(value, (list, dict)) else json.loads(value or "[]")

    @classmethod
    def _event_pg(cls, row, detail=False):
        event = {
            "uuid": row["uuid"], "lat": row["lat"], "lon": row["lon"],
            "description": row["description"], "event_date": row["event_date"].isoformat(),
            "timestamp": row["timestamp_text"], "time_precision": row["time_precision"],
            "faction_id": row["faction_id"], "faction_name": row["faction_name"],
            "faction_color": row["faction_color"], "icon_id": row["icon_id"],
            "icon_name": row["icon_name"],
            "icon_url": f"/api/geolocations/icons/{row['icon_id']}" if row["icon_id"] else None,
            "origin": row["origin"], "equipment": row["equipment"], "units": row["units"],
        }
        if detail:
            event.update({
                "evidence_links": cls._json(row["evidence_links"]),
                "geolocation_links": cls._json(row["geolocation_links"]),
                "plus_code": row["plus_code"],
                "gear_items": cls._json(row["gear_items"]),
                "orbat_units": cls._json(row["orbat_units"]),
                "attribution": {"name": "GeoConfirmed", "url": "https://geoconfirmed.org/ukraine"},
            })
        return event

    @staticmethod
    def _store_icons_pg(conn, icons):
        records = [item for item in icons.values() if item.get("icon_id")]
        with conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO geolocation_icons(id, name, upstream_path)
                VALUES (%(icon_id)s, %(icon_name)s, %(icon_path)s)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name, upstream_path = excluded.upstream_path
                """,
                records,
            )

    @staticmethod
    def _aware_timestamp(value):
        parsed = parse_timestamp(value)
        if parsed and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    @classmethod
    def _upsert_pg(cls, conn, records):
        items = []
        for record in records:
            item = dict(record)
            item["occurred_at"] = cls._aware_timestamp(item["timestamp"])
            for key in ("evidence_links", "geolocation_links", "gear_items", "orbat_units"):
                item[key] = json.dumps(item[key], ensure_ascii=False)
            items.append(item)
        with conn.cursor() as cursor:
            cursor.executemany(
                """
            INSERT INTO geolocation_events(
                uuid, event_date, timestamp_text, occurred_at, time_precision, location,
                description, faction_id, faction_name, faction_color, icon_id, icon_name,
                icon_path, origin, equipment, units, plus_code, evidence_links,
                geolocation_links, gear_items, orbat_units, source_hash, updated_at
            ) VALUES (
                %(uuid)s, %(event_date)s, %(timestamp)s, %(occurred_at)s, %(time_precision)s,
                ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), %(description)s,
                %(faction_id)s, %(faction_name)s, %(faction_color)s, %(icon_id)s,
                %(icon_name)s, %(icon_path)s, %(origin)s, %(equipment)s, %(units)s,
                %(plus_code)s, %(evidence_links)s::jsonb, %(geolocation_links)s::jsonb,
                %(gear_items)s::jsonb, %(orbat_units)s::jsonb, %(source_hash)s,
                to_timestamp(%(updated_at)s)
            ) ON CONFLICT(uuid) DO UPDATE SET
                event_date = excluded.event_date,
                timestamp_text = excluded.timestamp_text,
                occurred_at = excluded.occurred_at,
                time_precision = excluded.time_precision,
                location = excluded.location,
                description = excluded.description,
                faction_id = excluded.faction_id,
                faction_name = excluded.faction_name,
                faction_color = excluded.faction_color,
                icon_id = excluded.icon_id,
                icon_name = excluded.icon_name,
                icon_path = excluded.icon_path,
                origin = excluded.origin,
                equipment = excluded.equipment,
                units = excluded.units,
                plus_code = excluded.plus_code,
                evidence_links = excluded.evidence_links,
                geolocation_links = excluded.geolocation_links,
                gear_items = excluded.gear_items,
                orbat_units = excluded.orbat_units,
                source_hash = excluded.source_hash,
                updated_at = excluded.updated_at
                """,
                items,
            )

    def _full_sync(self, reconcile):
        if self.read_only or not self._sync_lock.acquire(False):
            return False
        lock_conn = None
        try:
            lock_conn = self._try_ingest_lock()
            if not lock_conn:
                return False
            rows = self.client.get_csv(self.retention_start, self.today() + timedelta(days=1))
            index, basic_icons = compact_index(self.client.get_json("/api/Placemark/Ukraine"))
            _, named_icons = compact_index(self.client.get_json("/api/Placemark/Ukraine/icons"))
            icons = {**basic_icons, **named_icons}
            records = self._merge_csv(rows, index, icons)
            with self.database.connect() as conn:
                # One transaction publishes icons, events, reconciliation removals,
                # and freshness metadata atomically.
                self._store_icons_pg(conn, icons)
                self._upsert_pg(conn, records)
                if reconcile:
                    retained = [item["uuid"] for item in records]
                    if retained:
                        conn.execute(
                            "DELETE FROM geolocation_events WHERE event_date >= %s AND NOT (uuid = ANY(%s))",
                            (self.retention_start, retained),
                        )
                    else:
                        conn.execute(
                            "DELETE FROM geolocation_events WHERE event_date >= %s",
                            (self.retention_start,),
                        )
                now = time.time()
                self._set_meta_pg(conn, "last_sync", now)
                self._set_meta_pg(conn, "last_reconcile", now)
                self._set_meta_pg(conn, "last_error", "")
            self._cache_icons()
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._record_sync_error(exc)
            log.warning("PostGIS GeoConfirmed full sync failed; serving stale data: %s", exc)
            return False
        finally:
            self._release_ingest_lock(lock_conn)
            self._sync_lock.release()

    def incremental_sync(self):
        if self.read_only or not self._sync_lock.acquire(False):
            return False
        lock_conn = None
        try:
            lock_conn = self._try_ingest_lock()
            if not lock_conn:
                return False
            start = max(self.retention_start, self.today() - timedelta(days=7))
            records = self._merge_csv(
                self.client.get_csv(start, self.today() + timedelta(days=1)), {}, {}
            )
            with self.database.connect(dict_rows=True) as conn:
                hashes = {
                    row["uuid"]: row["source_hash"]
                    for row in conn.execute(
                        "SELECT uuid, source_hash FROM geolocation_events WHERE event_date >= %s",
                        (start,),
                    )
                }
                icon_rows = {
                    row["upstream_path"]: row
                    for row in conn.execute(
                        "SELECT * FROM geolocation_icons WHERE upstream_path IS NOT NULL"
                    )
                }
                factions = {
                    row["faction_name"]: row
                    for row in conn.execute(
                        """
                        SELECT DISTINCT ON (faction_name) faction_name, faction_id, faction_color
                        FROM geolocation_events ORDER BY faction_name, updated_at DESC
                        """
                    )
                }
            changed = []
            for record in records:
                if hashes.get(record["uuid"]) != record["source_hash"]:
                    changed.append(self._merge_detail(
                        record,
                        self.client.get_json(f"/api/Placemark/detail/{record['uuid']}"),
                        icon_rows,
                        factions,
                    ))
            with self.database.connect() as conn:
                self._upsert_pg(conn, changed)
                self._set_meta_pg(conn, "last_sync", time.time())
                self._set_meta_pg(conn, "last_error", "")
            self._cache_icons()
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._record_sync_error(exc)
            log.warning("PostGIS GeoConfirmed incremental sync failed; serving stale data: %s", exc)
            return False
        finally:
            self._release_ingest_lock(lock_conn)
            self._sync_lock.release()

    def _cache_icons(self):
        with self.database.connect(dict_rows=True) as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT e.icon_id, e.icon_path, i.local_name
                FROM geolocation_events e
                LEFT JOIN geolocation_icons i ON i.id = e.icon_id
                WHERE e.icon_id IS NOT NULL AND e.icon_path IS NOT NULL
                """
            ).fetchall()
        for row in rows:
            if row["local_name"] and (self.icon_dir / row["local_name"]).is_file():
                continue
            try:
                body, mime = self.client.get_bytes(row["icon_path"])
                ext = mimetypes.guess_extension(mime or "") or Path(row["icon_path"]).suffix or ".png"
                name = f"{row['icon_id']}{ext}"
                tmp = self.icon_dir / f".{name}.tmp"
                tmp.write_bytes(body)
                tmp.replace(self.icon_dir / name)
                with self.database.connect() as conn:
                    conn.execute(
                        """
                        UPDATE geolocation_icons
                        SET local_name = %s, content_type = %s, updated_at = now()
                        WHERE id = %s
                        """,
                        (name, mime, row["icon_id"]),
                    )
            except Exception as exc:
                log.warning("Could not cache GeoConfirmed icon %s: %s", row["icon_id"], exc)

    def get_icon(self, icon_id):
        if not UUID_RE.fullmatch(icon_id):
            return None
        with self.database.connect(dict_rows=True) as conn:
            row = conn.execute(
                "SELECT local_name, content_type FROM geolocation_icons WHERE id = %s",
                (icon_id,),
            ).fetchone()
        if not row or not row["local_name"]:
            return None
        path = self.icon_dir / row["local_name"]
        return (path, row["content_type"] or "image/png") if path.is_file() else None

    def get_events_for_date(self, event_date, **filters):
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        clauses = [
            "event_date = %s",
            "location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)",
        ]
        values = [event_date, west, south, east, north]
        for key, column in {"faction": "faction_id", "icon": "icon_id", "origin": "origin"}.items():
            if filters.get(key):
                clauses.append(f"{column} = %s")
                values.append(str(filters[key]))
        if filters.get("q"):
            clauses.append("(strpos(lower(description), lower(%s)) > 0 OR strpos(lower(equipment), lower(%s)) > 0 OR strpos(lower(units), lower(%s)) > 0)")
            value = str(filters["q"])
            values.extend([value] * 3)
        with self.database.connect(dict_rows=True) as conn:
            rows = conn.execute(
                f"""
                SELECT geolocation_events.*, ST_Y(location) AS lat, ST_X(location) AS lon
                FROM geolocation_events WHERE {' AND '.join(clauses)}
                ORDER BY timestamp_text DESC, uuid
                """,
                values,
            ).fetchall()
        return [self._event_pg(row) for row in rows]

    def get_detail(self, uuid):
        if not UUID_RE.fullmatch(uuid):
            return None
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        with self.database.connect(dict_rows=True) as conn:
            row = conn.execute(
                """
                SELECT geolocation_events.*, ST_Y(location) AS lat, ST_X(location) AS lon
                FROM geolocation_events
                WHERE uuid = %s AND location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                """,
                (uuid, west, south, east, north),
            ).fetchone()
        return self._event_pg(row, True) if row else None

    def get_all(self, selected_date=None, **filters):
        if selected_date is not None and not re.fullmatch(r"\d{8}", selected_date):
            raise ValueError("date must use YYYYMMDD format")
        dates, cursor = [], self.retention_start
        while cursor <= self.today():
            dates.append(cursor.strftime("%Y%m%d"))
            cursor += timedelta(days=1)
        selected = selected_date or dates[-1]
        if selected not in dates:
            raise ValueError(f"geolocation date is not available: {selected}")
        selected_day = datetime.strptime(selected, "%Y%m%d").date()
        events = self.get_events_for_date(selected_day, **filters)
        south, north, west, east = UKRAINE_NEAR_BOUNDS
        with self.database.connect(dict_rows=True) as conn:
            count_rows = conn.execute(
                """
                SELECT event_date, count(*) AS count FROM geolocation_events
                WHERE location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                GROUP BY event_date
                """,
                (west, south, east, north),
            ).fetchall()
            counts = {row["event_date"].strftime("%Y%m%d"): row["count"] for row in count_rows}
            last_raw = self._meta_pg(conn, "last_sync")
            stored_error = self._meta_pg(conn, "last_error") or None
            total = conn.execute(
                """
                SELECT count(*) FROM geolocation_events
                WHERE location && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
                """,
                (west, south, east, north),
            ).fetchone()["count"]
        last = float(last_raw or 0) or None
        factions = sorted({(event["faction_id"], event["faction_name"], event["faction_color"]) for event in events})
        icons = sorted({(event["icon_id"], event["icon_name"] or "Uncategorized") for event in events if event["icon_id"]})
        origins = sorted({event["origin"] for event in events if event["origin"]})
        active_error = stored_error or self._last_error
        status = "stale" if active_error and total else "error" if active_error else "ok" if total else "empty"
        return {
            "date": selected, "dates": dates,
            "daily_counts": {day: counts.get(day, 0) for day in dates},
            "events": events,
            "filters": {
                "factions": [{"id": item[0], "name": item[1], "color": item[2]} for item in factions],
                "icons": [{"id": item[0], "name": item[1]} for item in icons],
                "origins": origins,
            },
            "sources": [{
                "id": self.id, "display_name": self.display_name,
                "event_count": len(events), "retained_event_count": total,
                "last_updated": last, "status": status,
            }],
        }


class GeolocationsService:
    def __init__(self, source=None, *, read_only=False, use_postgis=None):
        if source is None:
            try:
                from .database import postgis_enabled
            except ImportError:
                from database import postgis_enabled
            enabled = postgis_enabled() if use_postgis is None else use_postgis
            source = (
                PostGISGeoConfirmedGeolocationsSource(read_only=read_only)
                if enabled
                else GeoConfirmedGeolocationsSource()
            )
        self.source, self._thread, self._stop = source, None, threading.Event()
    def start(self):
        self.source.load_cache()
        if getattr(self.source, "read_only", False): return
        if self._thread and self._thread.is_alive(): return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="geoconfirmed-sync"); self._thread.start()
    def stop(self):
        self._stop.set()
        if self._thread: self._thread.join(timeout=5)
    def get_all(self, date=None, **filters): return self.source.get_all(selected_date=date, **filters)
    def get_detail(self, uuid): return self.source.get_detail(uuid)
    def get_icon(self, icon_id): return self.source.get_icon(icon_id)
    def _loop(self):
        while not self._stop.is_set():
            try: self.source.refresh_if_due()
            except Exception as exc: log.warning("GeoConfirmed sync loop error: %s", exc)
            self._stop.wait(60)
