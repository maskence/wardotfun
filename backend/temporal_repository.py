"""Immutable temporal map snapshots backed by PostgreSQL/PostGIS."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

try:
    from .database import PostGISDatabase
except ImportError:  # direct imports used by the current uvicorn deployment
    from database import PostGISDatabase

KYIV = ZoneInfo("Europe/Kyiv")
DATE_RE = re.compile(r"^\d{8}$")
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MVT_EXTENT = 4096
MVT_BUFFER = 64
WEB_MERCATOR_WIDTH = 40075016.68557849

STABLE_ID_FIELDS = (
    "globalid",
    "global_id",
    "objectid",
    "object_id",
    "fid",
    "placemark_id",
    "kml_id",
    "id",
)


class TemporalDataError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedFeature:
    logical_key: str
    identity_confidence: float
    content_hash: str
    geometry: dict[str, Any]
    properties: dict[str, Any]


@dataclass(frozen=True)
class IngestResult:
    run_id: int
    status: str
    snapshot_id: str | None
    content_hash: str
    feature_count: int


def parse_compact_date(value: str | None, *, default: date | None = None) -> date:
    if value is None:
        if default is None:
            raise TemporalDataError("date is required")
        return default
    if not DATE_RE.fullmatch(value):
        raise TemporalDataError("date must use YYYYMMDD format")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise TemporalDataError("date must be a real calendar date in YYYYMMDD format") from exc


def kyiv_calendar_date(captured_at: datetime) -> date:
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return captured_at.astimezone(KYIV).date()


def _normalized_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        rounded = round(value, 7)
        return 0.0 if rounded == -0.0 else rounded
    if isinstance(value, dict):
        return {
            str(key): _normalized_scalar(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalized_scalar(item) for item in value]
    return str(value)


def normalize_geometry(geometry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        raise TemporalDataError("feature geometry must be an object")
    geometry_type = geometry.get("type")
    if geometry_type not in {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }:
        raise TemporalDataError(f"unsupported GeoJSON geometry: {geometry_type!r}")
    if geometry_type == "GeometryCollection":
        geometries = geometry.get("geometries")
        if not isinstance(geometries, list):
            raise TemporalDataError("GeometryCollection.geometries must be an array")
        return {
            "type": geometry_type,
            "geometries": [normalize_geometry(item) for item in geometries],
        }
    if "coordinates" not in geometry:
        raise TemporalDataError("feature geometry is missing coordinates")
    coordinates = _normalized_scalar(geometry["coordinates"])
    return {"type": geometry_type, "coordinates": coordinates}


def canonical_json(value: Any) -> str:
    return json.dumps(
        _normalized_scalar(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _slug(value: Any) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return result[:160] or "unnamed"


def _property_lookup(properties: dict[str, Any], field: str) -> Any:
    for key, value in properties.items():
        if str(key).lower() == field and value not in (None, ""):
            return value
    return None


def logical_feature_key(
    source_id: str,
    layer_key: str,
    feature: dict[str, Any],
) -> tuple[str, float]:
    """Return a stable base key and an identity confidence score.

    ArcGIS/KML identifiers win. The fallback deliberately excludes geometry so
    a moved feature can retain its logical identity. Duplicate fallback keys are
    reconciled against the nearest previous geometry by ``ingest_overlay``.
    """
    properties = feature.get("properties") or {}
    top_level_id = feature.get("id")
    if top_level_id not in (None, ""):
        return f"{source_id}:{layer_key}:id:{_slug(top_level_id)}", 1.0
    for field in STABLE_ID_FIELDS:
        value = _property_lookup(properties, field)
        if value not in (None, ""):
            confidence = 1.0 if field in {"globalid", "global_id", "placemark_id", "kml_id"} else 0.95
            return f"{source_id}:{layer_key}:{field}:{_slug(value)}", confidence
    geometry_type = (feature.get("geometry") or {}).get("type", "Geometry")
    name = _property_lookup(properties, "name") or _property_lookup(properties, "title")
    return (
        f"{source_id}:{layer_key}:fallback:{_slug(name)}:{_slug(geometry_type)}",
        0.65,
    )


def _coordinate_pairs(value: Any) -> Iterable[tuple[float, float]]:
    if (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    ):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, list):
        for item in value:
            yield from _coordinate_pairs(item)


def geometry_centroid(geometry: dict[str, Any]) -> tuple[float, float]:
    if geometry.get("type") == "GeometryCollection":
        points = [
            point
            for child in geometry.get("geometries", [])
            for point in _coordinate_pairs(child.get("coordinates", []))
        ]
    else:
        points = list(_coordinate_pairs(geometry.get("coordinates", [])))
    if not points:
        return (0.0, 0.0)
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def geometry_bounds(geometries: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    points = [
        point
        for geometry in geometries
        for point in (
            [
                nested
                for child in geometry.get("geometries", [])
                for nested in _coordinate_pairs(child.get("coordinates", []))
            ]
            if geometry.get("type") == "GeometryCollection"
            else list(_coordinate_pairs(geometry.get("coordinates", [])))
        )
    ]
    if not points:
        return None
    west = min(point[0] for point in points)
    east = max(point[0] for point in points)
    south = min(point[1] for point in points)
    north = max(point[1] for point in points)
    return {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }


def normalized_overlay(payload: dict[str, Any]) -> dict[str, Any]:
    """Canonical snapshot material, independent of upstream feature ordering."""
    layers = []
    for ordinal, layer in enumerate(payload.get("layers") or []):
        feature_material = []
        for feature in (layer.get("data") or {}).get("features") or []:
            geometry = normalize_geometry(feature.get("geometry"))
            properties = _normalized_scalar(feature.get("properties") or {})
            feature_material.append({"geometry": geometry, "properties": properties})
        feature_material.sort(key=content_hash)
        layers.append(
            {
                "id": str(layer.get("id") or f"layer-{ordinal}"),
                "label": str(layer.get("label") or layer.get("id") or f"Layer {ordinal + 1}"),
                "geom_type": str(layer.get("geom_type") or "polygon"),
                "paint": _normalized_scalar(layer.get("paint") or {}),
                "features": feature_material,
            }
        )
    layers.sort(key=lambda layer: layer["id"])
    return {"source_id": payload.get("mapper_id"), "layers": layers}


class TemporalMapRepository:
    def __init__(self, database: PostGISDatabase | None = None):
        self.database = database or PostGISDatabase()

    def register_source(
        self,
        *,
        source_id: str,
        kind: str,
        display_name: str,
        source_url: str | None,
        attribution: str,
        upstream_type: str,
        upstream_config: dict[str, Any] | None = None,
        refresh_policy: dict[str, Any] | None = None,
    ) -> None:
        if not SOURCE_RE.fullmatch(source_id):
            raise TemporalDataError(f"invalid source id: {source_id!r}")
        if kind not in {"mapper", "fortifications"}:
            raise TemporalDataError(f"invalid source kind: {kind!r}")
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO map_sources(
                    id, kind, display_name, source_url, attribution,
                    upstream_type, upstream_config, refresh_policy
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT(id) DO UPDATE SET
                    kind = excluded.kind,
                    display_name = excluded.display_name,
                    source_url = excluded.source_url,
                    attribution = excluded.attribution,
                    upstream_type = excluded.upstream_type,
                    upstream_config = excluded.upstream_config,
                    refresh_policy = excluded.refresh_policy,
                    updated_at = now()
                """,
                (
                    source_id,
                    kind,
                    display_name,
                    source_url,
                    attribution,
                    upstream_type,
                    canonical_json(upstream_config or {}),
                    canonical_json(refresh_policy or {}),
                ),
            )

    def _start_run(self, source_id: str) -> int:
        with self.database.connect() as conn:
            return conn.execute(
                "INSERT INTO ingest_runs(source_id, status) VALUES (%s, 'running') RETURNING id",
                (source_id,),
            ).fetchone()[0]

    def record_failure(
        self,
        source_id: str,
        error: str,
        *,
        raw_records: list[dict[str, Any]] | None = None,
    ) -> int:
        run_id = self._start_run(source_id)
        self._finish_run(
            run_id,
            status="failed",
            error=error[:4000],
            raw_records=raw_records,
        )
        return run_id

    def _finish_run(
        self,
        run_id: int,
        *,
        status: str,
        normalized_hash: str | None = None,
        snapshot_id: str | None = None,
        feature_count: int | None = None,
        error: str | None = None,
        raw_records: list[dict[str, Any]] | None = None,
    ) -> None:
        raw_records = raw_records or []
        first = raw_records[0] if raw_records else {}
        with self.database.connect() as conn:
            for raw in raw_records:
                conn.execute(
                    """
                    INSERT INTO raw_archives(sha256, path, content_type, byte_count)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT(sha256) DO NOTHING
                    """,
                    (
                        raw["sha256"], raw["path"], raw.get("content_type"),
                        raw["byte_count"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ingest_run_raw_archives(ingest_run_id, raw_sha256, upstream_url)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (run_id, raw["sha256"], raw["url"]),
                )
            conn.execute(
                """
                UPDATE ingest_runs SET
                    finished_at = now(), status = %s, normalized_hash = %s,
                    snapshot_id = %s, feature_count = %s, error = %s,
                    raw_content_hash = %s, raw_path = %s,
                    http_etag = %s, last_modified = %s
                WHERE id = %s
                """,
                (
                    status,
                    normalized_hash,
                    snapshot_id,
                    feature_count,
                    error,
                    first.get("sha256"),
                    first.get("path"),
                    first.get("etag"),
                    first.get("last_modified"),
                    run_id,
                ),
            )

    @staticmethod
    def _previous_candidates(conn, source_id: str, bases: list[str]) -> dict[str, list[dict[str, Any]]]:
        if not bases:
            return {}
        rows = conn.execute(
            """
            WITH latest AS (
                SELECT id FROM map_snapshots
                WHERE source_id = %s
                ORDER BY captured_at DESC, created_at DESC, id DESC LIMIT 1
            )
            SELECT fv.logical_key,
                   ST_X(ST_Centroid(fv.geometry)) AS lon,
                   ST_Y(ST_Centroid(fv.geometry)) AS lat
            FROM latest
            JOIN snapshot_features sf ON sf.snapshot_id = latest.id
            JOIN feature_versions fv ON fv.id = sf.feature_version_id
            WHERE split_part(fv.logical_key, '#', 1) = ANY(%s)
            """,
            (source_id, bases),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for logical_key, lon, lat in rows:
            grouped[logical_key.split("#", 1)[0]].append(
                {"key": logical_key, "centroid": (float(lon), float(lat))}
            )
        return grouped

    def _prepare_layers(self, conn, source_id: str, payload: dict[str, Any]):
        provisional: list[dict[str, Any]] = []
        for ordinal, layer in enumerate(payload.get("layers") or []):
            layer_key = str(layer.get("id") or f"layer-{ordinal}")
            features = []
            for feature in (layer.get("data") or {}).get("features") or []:
                geometry = normalize_geometry(feature.get("geometry"))
                properties = _normalized_scalar(feature.get("properties") or {})
                normalized_feature = {
                    "id": feature.get("id"),
                    "geometry": geometry,
                    "properties": properties,
                }
                base, confidence = logical_feature_key(source_id, layer_key, normalized_feature)
                digest = content_hash({"geometry": geometry, "properties": properties})
                features.append(
                    {
                        "base": base,
                        "confidence": confidence,
                        "content_hash": digest,
                        "geometry": geometry,
                        "properties": properties,
                        "centroid": geometry_centroid(geometry),
                    }
                )
            provisional.append(
                {
                    "key": layer_key,
                    "label": str(layer.get("label") or layer_key),
                    "geometry_type": str(layer.get("geom_type") or "polygon"),
                    "paint": _normalized_scalar(layer.get("paint") or {}),
                    "ordinal": ordinal,
                    "features": features,
                }
            )

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for layer in provisional:
            for feature in layer["features"]:
                groups[feature["base"]].append(feature)
        duplicate_bases = [base for base, items in groups.items() if len(items) > 1]
        previous = self._previous_candidates(conn, source_id, duplicate_bases)

        for base, items in groups.items():
            if len(items) == 1:
                items[0]["logical_key"] = base
                continue
            available = list(previous.get(base, []))
            stable_group = all(item["confidence"] >= 0.95 for item in items)
            used_keys: set[str] = set()
            # Deterministic input order makes matching/replays reproducible.
            for item in sorted(items, key=lambda value: value["content_hash"]):
                if available:
                    lon, lat = item["centroid"]
                    nearest = min(
                        available,
                        key=lambda old: (old["centroid"][0] - lon) ** 2
                        + (old["centroid"][1] - lat) ** 2,
                    )
                    available.remove(nearest)
                    item["logical_key"] = nearest["key"]
                    item["confidence"] = min(item["confidence"], 0.9 if stable_group else 0.55)
                else:
                    stem = f"{base}#{item['content_hash'][:16]}"
                    candidate = stem
                    occurrence = 2
                    while candidate in used_keys:
                        candidate = f"{stem}-{occurrence}"
                        occurrence += 1
                    item["logical_key"] = candidate
                    item["confidence"] = min(item["confidence"], 0.85 if stable_group else 0.45)
                used_keys.add(item["logical_key"])

        return provisional

    def ingest_overlay(
        self,
        payload: dict[str, Any],
        *,
        captured_at: datetime | None = None,
        raw_records: list[dict[str, Any]] | None = None,
    ) -> IngestResult:
        source_id = str(payload.get("mapper_id") or "")
        if not SOURCE_RE.fullmatch(source_id):
            raise TemporalDataError("overlay mapper_id is missing or invalid")
        captured_at = captured_at or datetime.now(timezone.utc)
        if captured_at.tzinfo is None:
            captured_at = captured_at.replace(tzinfo=timezone.utc)
        material = normalized_overlay(payload)
        digest = content_hash(material)
        count = sum(len(layer["features"]) for layer in material["layers"])
        run_id = self._start_run(source_id)

        try:
            with self.database.connect(dict_rows=True) as conn:
                locked = conn.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired",
                    (f"wardotfun:ingest:{source_id}",),
                ).fetchone()["acquired"]
                if not locked:
                    conn.rollback()
                    self._finish_run(
                        run_id,
                        status="locked",
                        normalized_hash=digest,
                        feature_count=count,
                        raw_records=raw_records,
                    )
                    return IngestResult(run_id, "locked", None, digest, count)

                existing = conn.execute(
                    "SELECT id FROM map_snapshots WHERE source_id = %s AND content_hash = %s",
                    (source_id, digest),
                ).fetchone()
                if existing:
                    conn.commit()
                    snapshot_id = str(existing["id"])
                    self._finish_run(
                        run_id,
                        status="unchanged",
                        normalized_hash=digest,
                        snapshot_id=snapshot_id,
                        feature_count=count,
                        raw_records=raw_records,
                    )
                    return IngestResult(run_id, "unchanged", snapshot_id, digest, count)

                prepared_layers = self._prepare_layers(conn, source_id, payload)
                all_geometries = [
                    feature["geometry"]
                    for layer in prepared_layers
                    for feature in layer["features"]
                ]
                bounds = geometry_bounds(all_geometries)
                snapshot_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO map_snapshots(
                        id, source_id, ingest_run_id, captured_at, calendar_date,
                        content_hash, feature_count, bounds
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s::text IS NULL THEN NULL
                             ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s::text), 4326) END
                    )
                    """,
                    (
                        snapshot_id,
                        source_id,
                        run_id,
                        captured_at,
                        kyiv_calendar_date(captured_at),
                        digest,
                        count,
                        canonical_json(bounds) if bounds else None,
                        canonical_json(bounds) if bounds else None,
                    ),
                )
                for layer in prepared_layers:
                    layer_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO map_layer_versions(
                            id, snapshot_id, layer_key, label, geometry_type,
                            paint, feature_count, ordinal
                        ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                        """,
                        (
                            layer_id,
                            snapshot_id,
                            layer["key"],
                            layer["label"],
                            layer["geometry_type"],
                            canonical_json(layer["paint"]),
                            len(layer["features"]),
                            layer["ordinal"],
                        ),
                    )
                    for feature in layer["features"]:
                        feature_id = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"wardotfun:{source_id}:{feature['logical_key']}:{feature['content_hash']}",
                            )
                        )
                        conn.execute(
                            """
                            INSERT INTO feature_versions(
                                id, source_id, logical_key, identity_confidence,
                                content_hash, geometry, properties
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                ST_MakeValid(ST_Force2D(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))),
                                %s::jsonb
                            ) ON CONFLICT(source_id, logical_key, content_hash) DO NOTHING
                            """,
                            (
                                feature_id,
                                source_id,
                                feature["logical_key"],
                                feature["confidence"],
                                feature["content_hash"],
                                canonical_json(feature["geometry"]),
                                canonical_json(feature["properties"]),
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO snapshot_features(
                                snapshot_id, layer_version_id, feature_version_id
                            ) VALUES (%s, %s, %s)
                            """,
                            (snapshot_id, layer_id, feature_id),
                        )
                conn.commit()

            self._finish_run(
                run_id,
                status="stored",
                normalized_hash=digest,
                snapshot_id=snapshot_id,
                feature_count=count,
                raw_records=raw_records,
            )
            return IngestResult(run_id, "stored", snapshot_id, digest, count)
        except Exception as exc:
            self._finish_run(
                run_id,
                status="failed",
                normalized_hash=digest,
                feature_count=count,
                error=str(exc)[:4000],
                raw_records=raw_records,
            )
            raise

    def get_map_state(self, selected: str | None = None) -> dict[str, Any]:
        today = datetime.now(KYIV).date()
        selected_date = parse_compact_date(selected, default=today)
        with self.database.connect(dict_rows=True) as conn:
            first_snapshot = conn.execute(
                "SELECT min(calendar_date) AS value FROM map_snapshots"
            ).fetchone()["value"]
            retention_row = conn.execute(
                "SELECT value FROM geolocation_metadata WHERE key = 'retention_start'"
            ).fetchone()
            first_event = conn.execute(
                "SELECT min(event_date) AS value FROM geolocation_events"
            ).fetchone()["value"]
            candidates = [value for value in (first_snapshot, first_event) if value]
            if retention_row:
                try:
                    candidates.append(date.fromisoformat(retention_row["value"]))
                except ValueError:
                    pass
            start = min(candidates) if candidates else today
            if selected_date < start or selected_date > today:
                raise TemporalDataError(
                    f"map date is not available: {selected_date.strftime('%Y%m%d')}"
                )

            rows = conn.execute(
                """
                SELECT s.id, s.kind, s.display_name, s.source_url, s.attribution,
                       snap.id AS snapshot_id, snap.captured_at,
                       snap.calendar_date AS snapshot_date,
                       snap.feature_count, snap.content_hash,
                       latest_run.status AS ingest_status,
                       latest_run.finished_at AS ingest_finished_at,
                       latest_run.error AS ingest_error
                FROM map_sources s
                LEFT JOIN LATERAL (
                    SELECT ms.* FROM map_snapshots ms
                    WHERE ms.source_id = s.id
                      AND ms.captured_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Europe/Kyiv')
                    ORDER BY ms.captured_at DESC, ms.created_at DESC, ms.id DESC LIMIT 1
                ) snap ON true
                LEFT JOIN LATERAL (
                    SELECT ir.status, ir.finished_at, ir.error FROM ingest_runs ir
                    WHERE ir.source_id = s.id
                    ORDER BY ir.started_at DESC LIMIT 1
                ) latest_run ON true
                WHERE s.enabled
                ORDER BY s.kind, s.id
                """,
                (selected_date,),
            ).fetchall()
            snapshot_ids = [row["snapshot_id"] for row in rows if row["snapshot_id"]]
            layers_by_snapshot: dict[str, list[dict[str, Any]]] = defaultdict(list)
            if snapshot_ids:
                for layer in conn.execute(
                    """
                    SELECT snapshot_id, layer_key, label, geometry_type, paint, feature_count
                    FROM map_layer_versions
                    WHERE snapshot_id = ANY(%s)
                    ORDER BY snapshot_id, ordinal
                    """,
                    (snapshot_ids,),
                ).fetchall():
                    layers_by_snapshot[str(layer["snapshot_id"])].append(
                        {
                            "id": layer["layer_key"],
                            "label": layer["label"],
                            "geom_type": layer["geometry_type"],
                            "paint": layer["paint"],
                            "feature_count": layer["feature_count"],
                            "source_layer": "features",
                        }
                    )
            geo_count = conn.execute(
                "SELECT count(*) AS count FROM geolocation_events WHERE event_date = %s",
                (selected_date,),
            ).fetchone()["count"]
            geo_last = conn.execute(
                "SELECT value FROM geolocation_metadata WHERE key = 'last_sync'"
            ).fetchone()
            geo_error = conn.execute(
                "SELECT value FROM geolocation_metadata WHERE key = 'last_error'"
            ).fetchone()

        available_dates = []
        cursor = start
        while cursor <= today:
            available_dates.append(cursor.strftime("%Y%m%d"))
            cursor += timedelta(days=1)

        sources = []
        for row in rows:
            snapshot_id = str(row["snapshot_id"]) if row["snapshot_id"] else None
            captured = row["captured_at"]
            latest_status = row["ingest_status"]
            if not snapshot_id:
                status = "unavailable"
            elif latest_status == "failed":
                status = "stale"
            else:
                status = "ok"
            item = {
                "id": row["id"],
                "mapper_id": row["id"],
                "kind": row["kind"],
                "display_name": row["display_name"],
                "source_url": row["source_url"],
                "attribution": row["attribution"],
                "snapshot_id": snapshot_id,
                "snapshot_date": row["snapshot_date"].strftime("%Y%m%d") if row["snapshot_date"] else None,
                "captured_at": captured.isoformat() if captured else None,
                "last_updated": captured.timestamp() if captured else None,
                "feature_count": row["feature_count"] or 0,
                "content_hash": row["content_hash"],
                "status": status,
                "available": bool(snapshot_id),
                "layers": layers_by_snapshot.get(snapshot_id, []),
                "tile_url": (
                    f"/api/map-tiles/{row['id']}/{snapshot_id}/{{z}}/{{x}}/{{y}}.pbf"
                    if snapshot_id else None
                ),
                "freshness": {
                    "ingest_status": latest_status,
                    "checked_at": row["ingest_finished_at"].isoformat() if row["ingest_finished_at"] else None,
                    "error": row["ingest_error"],
                },
            }
            sources.append(item)

        mappers = [item for item in sources if item["kind"] == "mapper"]
        fortifications = next(
            (item for item in sources if item["kind"] == "fortifications"), None
        )
        return {
            "date": selected_date.strftime("%Y%m%d"),
            "timezone": "Europe/Kyiv",
            "available_dates": available_dates,
            "vector_tiles_enabled": True,
            "mappers": mappers,
            "fortifications": fortifications,
            "geoconfirmed": {
                "date": selected_date.strftime("%Y%m%d"),
                "event_count": geo_count,
                "available": selected_date >= start,
                "last_updated": geo_last["value"] if geo_last else None,
                "status": "stale" if geo_error and geo_error["value"] else "ok",
                "error": geo_error["value"] if geo_error and geo_error["value"] else None,
            },
        }

    def get_tile(
        self,
        source_id: str,
        snapshot_id: str,
        z: int,
        x: int,
        y: int,
    ) -> tuple[bytes, str]:
        if not SOURCE_RE.fullmatch(source_id):
            raise TemporalDataError("invalid source")
        try:
            parsed_snapshot = str(uuid.UUID(snapshot_id))
        except (ValueError, TypeError) as exc:
            raise TemporalDataError("invalid snapshot") from exc
        if not 0 <= z <= 22:
            raise TemporalDataError("zoom must be between 0 and 22")
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            raise TemporalDataError("tile coordinate is outside the zoom grid")
        tile_width = WEB_MERCATOR_WIDTH / limit
        simplify_tolerance = tile_width / (MVT_EXTENT * 2)
        buffer_width = tile_width * MVT_BUFFER / MVT_EXTENT
        with self.database.connect() as conn:
            owner = conn.execute(
                "SELECT 1 FROM map_snapshots WHERE id = %s AND source_id = %s",
                (parsed_snapshot, source_id),
            ).fetchone()
            if not owner:
                raise KeyError("snapshot not found for source")
            row = conn.execute(
                """
                WITH tile_bounds AS (
                    SELECT ST_TileEnvelope(%s, %s, %s) AS geom
                ), tile_rows AS (
                    SELECT
                        (('x' || substr(md5(fv.id::text), 1, 15))::bit(60)::bigint) AS mvt_id,
                        mlv.layer_key,
                        fv.logical_key,
                        fv.identity_confidence,
                        fv.properties,
                        ST_AsMVTGeom(
                            CASE WHEN %s < 12
                                THEN ST_SimplifyPreserveTopology(
                                    ST_Transform(fv.geometry, 3857), %s
                                )
                                ELSE ST_Transform(fv.geometry, 3857)
                            END,
                            tile_bounds.geom,
                            %s,
                            %s,
                            true
                        ) AS geom
                    FROM tile_bounds
                    JOIN snapshot_features sf ON sf.snapshot_id = %s
                    JOIN map_layer_versions mlv ON mlv.id = sf.layer_version_id
                    JOIN feature_versions fv ON fv.id = sf.feature_version_id
                    WHERE fv.geometry && ST_Transform(
                        ST_Expand(tile_bounds.geom, %s), 4326
                    )
                )
                SELECT ST_AsMVT(tile_rows, 'features', %s, 'geom', 'mvt_id')
                FROM tile_rows WHERE geom IS NOT NULL
                """,
                (
                    z,
                    x,
                    y,
                    z,
                    simplify_tolerance,
                    MVT_EXTENT,
                    MVT_BUFFER,
                    parsed_snapshot,
                    buffer_width,
                    MVT_EXTENT,
                ),
            ).fetchone()
        tile = bytes(row[0]) if row and row[0] is not None else b""
        etag = '"' + hashlib.sha256(
            f"mvt-v1:{source_id}:{parsed_snapshot}:{z}:{x}:{y}".encode("ascii")
        ).hexdigest() + '"'
        return tile, etag

    def health(self) -> dict[str, Any]:
        with self.database.connect(dict_rows=True) as conn:
            row = conn.execute(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'failed' AND started_at > now() - interval '24 hours') AS failed_runs,
                    max(finished_at) FILTER (WHERE status IN ('stored', 'unchanged', 'not_modified')) AS last_success
                FROM ingest_runs
                """
            ).fetchone()
        return {
            "status": "ok" if not row["failed_runs"] else "degraded",
            "failed_ingest_runs_24h": row["failed_runs"],
            "last_ingest_success": row["last_success"].isoformat() if row["last_success"] else None,
        }
