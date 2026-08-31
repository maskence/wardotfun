"""Immutable temporal map snapshots backed by PostgreSQL/PostGIS."""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
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
    observation_id: str | None = None


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


def encode_change_cursor(observed_at: datetime, area_id: str) -> str:
    raw = canonical_json([observed_at.isoformat(), str(area_id)]).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_change_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        raw = urlsafe_b64decode(value + "=" * (-len(value) % 4))
        timestamp, area_id = json.loads(raw)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed, str(uuid.UUID(area_id))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise TemporalDataError("invalid change-feed cursor") from exc


def _mercator_xy(lon: float, lat: float) -> tuple[float, float]:
    lat = max(-85.05112878, min(85.05112878, lat))
    x = math.radians(lon) * 6378137.0
    y = 6378137.0 * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
    return x, y


def _svg_geometry(geometry: dict[str, Any], project) -> str:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Point":
        x, y = project(coordinates)
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5"/>'
    if geometry_type == "MultiPoint":
        return "".join(
            f'<circle cx="{project(point)[0]:.1f}" cy="{project(point)[1]:.1f}" r="5"/>'
            for point in coordinates
        )

    def path(points, close=False):
        if not points:
            return ""
        projected = [project(point) for point in points]
        commands = [f"M{projected[0][0]:.1f},{projected[0][1]:.1f}"]
        commands.extend(f"L{x:.1f},{y:.1f}" for x, y in projected[1:])
        if close:
            commands.append("Z")
        return " ".join(commands)

    paths: list[str] = []
    if geometry_type == "LineString":
        paths.append(path(coordinates))
    elif geometry_type == "MultiLineString":
        paths.extend(path(line) for line in coordinates)
    elif geometry_type == "Polygon":
        paths.extend(path(ring, True) for ring in coordinates)
    elif geometry_type == "MultiPolygon":
        paths.extend(path(ring, True) for polygon in coordinates for ring in polygon)
    elif geometry_type == "GeometryCollection":
        return "".join(_svg_geometry(child, project) for child in geometry.get("geometries", []))
    return "".join(f'<path d="{value}"/>' for value in paths if value)


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


def geometry_bounds_center(geometry: dict[str, Any]) -> tuple[float, float]:
    """Return the bounding-box center used by PostGIS candidate matching."""
    if geometry.get("type") == "GeometryCollection":
        points = [point for child in geometry.get("geometries", []) for point in _coordinate_pairs(child.get("coordinates", []))]
    else:
        points = list(_coordinate_pairs(geometry.get("coordinates", [])))
    if not points:
        return (0.0, 0.0)
    return (
        (min(point[0] for point in points) + max(point[0] for point in points)) / 2,
        (min(point[1] for point in points) + max(point[1] for point in points)) / 2,
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
                SELECT snapshot_id AS id FROM map_snapshot_observations
                WHERE source_id = %s
                ORDER BY observed_at DESC, id DESC LIMIT 1
            )
            SELECT fv.logical_key, fv.content_hash,
                   (ST_XMin(box3d(fv.geometry)) + ST_XMax(box3d(fv.geometry))) / 2 AS lon,
                   (ST_YMin(box3d(fv.geometry)) + ST_YMax(box3d(fv.geometry))) / 2 AS lat
            FROM latest
            JOIN snapshot_features sf ON sf.snapshot_id = latest.id
            JOIN feature_versions fv ON fv.id = sf.feature_version_id
            WHERE split_part(fv.logical_key, '#', 1) = ANY(%s)
            """,
            (source_id, bases),
        ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            logical_key = row["logical_key"]
            grouped[logical_key.split("#", 1)[0]].append(
                {"key": logical_key, "content_hash": row["content_hash"], "center": (float(row["lon"]), float(row["lat"]))}
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
                        "center": geometry_bounds_center(geometry),
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
            # KML may reorder same-name placemarks. Preserve exact content first.
            ordered = sorted(items, key=lambda value: value["content_hash"])
            unmatched = []
            for item in ordered:
                exact = next((old for old in available if old["content_hash"] == item["content_hash"]), None)
                if exact is None:
                    unmatched.append(item)
                    continue
                available.remove(exact)
                item["logical_key"] = exact["key"]
                item["confidence"] = min(item["confidence"], 0.9 if stable_group else 0.65)
                used_keys.add(item["logical_key"])

            # Spatially reconcile only features whose actual content changed.
            for item in unmatched:
                if available:
                    lon, lat = item["center"]
                    nearest = min(
                        available,
                        key=lambda old: (old["center"][0] - lon) ** 2
                        + (old["center"][1] - lat) ** 2,
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

    @staticmethod
    def _create_change_areas(conn, observation_id: str, style_count: int) -> None:
        clusters = conn.execute(
            """
            WITH pieces AS (
                SELECT mcf.logical_key, mcf.change_type, dumped.geom AS bounds
                FROM map_change_features mcf
                CROSS JOIN LATERAL ST_Dump(mcf.bounds) dumped
                WHERE mcf.observation_id = %s
                  AND NOT ST_IsEmpty(dumped.geom)
            ), clustered AS (
                SELECT pieces.*,
                       ST_ClusterDBSCAN(
                           ST_Transform(ST_PointOnSurface(bounds), 3857), 50000, 1
                       ) OVER (ORDER BY logical_key, change_type, md5(ST_AsEWKB(bounds))) AS cluster_id
                FROM pieces
            ), members AS (
                SELECT DISTINCT cluster_id, logical_key, change_type FROM clustered
            ), grouped AS (
                SELECT clustered.cluster_id, ST_Envelope(ST_Collect(clustered.bounds)) AS bounds,
                       count(DISTINCT (members.logical_key, members.change_type)) FILTER (WHERE members.change_type = 'added')::integer AS added_count,
                       count(DISTINCT (members.logical_key, members.change_type)) FILTER (WHERE members.change_type = 'removed')::integer AS removed_count,
                       count(DISTINCT (members.logical_key, members.change_type)) FILTER (WHERE members.change_type = 'modified')::integer AS modified_count,
                       jsonb_agg(DISTINCT jsonb_build_array(members.logical_key, members.change_type)
                                 ORDER BY jsonb_build_array(members.logical_key, members.change_type)) AS members
                FROM clustered
                JOIN members USING(cluster_id, logical_key, change_type)
                GROUP BY clustered.cluster_id
            )
            SELECT ST_AsGeoJSON(bounds) AS bounds, added_count, removed_count,
                   modified_count, members
            FROM grouped
            ORDER BY ST_XMin(box3d(bounds)), ST_YMin(box3d(bounds)),
                     ST_XMax(box3d(bounds)), ST_YMax(box3d(bounds))
            """,
            (observation_id,),
        ).fetchall()
        ordinal = 0
        for row in clusters:
            area_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wardotfun:change:v2:{observation_id}:{ordinal}"))
            conn.execute(
                """
                INSERT INTO map_change_areas(
                    id, observation_id, ordinal, bounds,
                    added_count, removed_count, modified_count, style_count
                ) VALUES (
                    %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326),
                    %s, %s, %s, 0
                )
                """,
                (
                    area_id, observation_id, ordinal, row["bounds"],
                    row["added_count"], row["removed_count"], row["modified_count"],
                ),
            )
            with conn.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO map_change_area_features(
                        area_id, observation_id, logical_key, change_type
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    [(area_id, observation_id, member[0], member[1]) for member in row["members"]],
                )
            ordinal += 1
        if style_count:
            row = conn.execute(
                """
                SELECT ST_AsGeoJSON(ST_Envelope(COALESCE(n.bounds, o.bounds))) AS bounds
                FROM map_snapshot_observations observation
                LEFT JOIN map_snapshots n ON n.id = observation.snapshot_id
                LEFT JOIN map_snapshots o ON o.id = observation.previous_snapshot_id
                WHERE observation.id = %s
                """,
                (observation_id,),
            ).fetchone()
            if row and row["bounds"]:
                area_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wardotfun:change:v2:{observation_id}:{ordinal}"))
                conn.execute(
                    """
                    INSERT INTO map_change_areas(
                        id, observation_id, ordinal, bounds, style_count
                    ) VALUES (
                        %s, %s, %s, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), %s
                    )
                    """,
                    (area_id, observation_id, ordinal, row["bounds"], style_count),
                )

    def _create_observation(
        self,
        conn,
        *,
        source_id: str,
        snapshot_id: str,
        previous_snapshot_id: str | None,
        observed_at: datetime,
        ingest_run_id: int | None,
    ) -> str:
        observation_id = str(uuid.uuid4())
        baseline = previous_snapshot_id is None
        if baseline:
            stats = {
                "added_count": 0, "removed_count": 0, "modified_count": 0,
                "style_count": 0, "bounds": None,
            }
        else:
            stats = conn.execute(
                """
                WITH feature_stats AS (
                    SELECT count(*) FILTER (WHERE change_type = 'added')::integer AS added_count,
                           count(*) FILTER (WHERE change_type = 'removed')::integer AS removed_count,
                           count(*) FILTER (WHERE change_type = 'modified')::integer AS modified_count,
                           ST_Envelope(ST_Collect(bounds)) AS bounds
                    FROM map_snapshot_feature_diff(%s, %s)
                ), old_layers AS (
                    SELECT layer_key, label, geometry_type, paint
                    FROM map_layer_versions WHERE snapshot_id = %s
                ), new_layers AS (
                    SELECT layer_key, label, geometry_type, paint
                    FROM map_layer_versions WHERE snapshot_id = %s
                ), style_stats AS (
                    SELECT count(*)::integer AS style_count
                    FROM old_layers o FULL JOIN new_layers n USING(layer_key)
                    WHERE o.layer_key IS NULL OR n.layer_key IS NULL
                       OR o.label IS DISTINCT FROM n.label
                       OR o.geometry_type IS DISTINCT FROM n.geometry_type
                       OR o.paint IS DISTINCT FROM n.paint
                )
                SELECT feature_stats.added_count, feature_stats.removed_count,
                       feature_stats.modified_count, style_stats.style_count,
                       ST_AsGeoJSON(feature_stats.bounds) AS bounds
                FROM feature_stats CROSS JOIN style_stats
                """,
                (previous_snapshot_id, snapshot_id, previous_snapshot_id, snapshot_id),
            ).fetchone()
        conn.execute(
            """
            INSERT INTO map_snapshot_observations(
                id, source_id, snapshot_id, previous_snapshot_id, ingest_run_id,
                observed_at, calendar_date, is_baseline,
                added_count, removed_count, modified_count, style_count, bounds
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                CASE WHEN %s::text IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) END
            )
            """,
            (
                observation_id, source_id, snapshot_id, previous_snapshot_id,
                ingest_run_id, observed_at, kyiv_calendar_date(observed_at), baseline,
                stats["added_count"], stats["removed_count"], stats["modified_count"],
                stats["style_count"], stats["bounds"], stats["bounds"],
            ),
        )
        if not baseline:
            conn.execute(
                """
                INSERT INTO map_change_features(
                    observation_id, logical_key, change_type,
                    old_feature_version_id, new_feature_version_id,
                    old_layer_key, new_layer_key, identity_confidence, bounds
                )
                SELECT %s, logical_key, change_type, old_feature_version_id,
                       new_feature_version_id, old_layer_key, new_layer_key,
                       identity_confidence, bounds
                FROM map_snapshot_feature_diff(%s, %s)
                """,
                (observation_id, previous_snapshot_id, snapshot_id),
            )
            self._create_change_areas(conn, observation_id, stats["style_count"])
        return observation_id

    def backfill_change_observations(self) -> dict[str, int]:
        """Idempotently reconstruct the observable timeline from stored snapshots."""
        created_observations = 0
        created_areas = 0
        with self.database.connect(dict_rows=True) as conn:
            sources = conn.execute("SELECT id FROM map_sources ORDER BY id").fetchall()
            for source in sources:
                source_id = source["id"]
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"wardotfun:ingest:{source_id}",),
                )
                existing = conn.execute(
                    "SELECT count(*) AS count FROM map_snapshot_observations WHERE source_id = %s",
                    (source_id,),
                ).fetchone()["count"]
                if existing:
                    conn.commit()
                    continue
                snapshots = conn.execute(
                    """
                    SELECT id, ingest_run_id, captured_at FROM map_snapshots
                    WHERE source_id = %s
                    ORDER BY captured_at, created_at, id
                    """,
                    (source_id,),
                ).fetchall()
                previous = None
                for snapshot in snapshots:
                    observation_id = self._create_observation(
                        conn,
                        source_id=source_id,
                        snapshot_id=str(snapshot["id"]),
                        previous_snapshot_id=previous,
                        observed_at=snapshot["captured_at"],
                        ingest_run_id=snapshot["ingest_run_id"],
                    )
                    created_observations += 1
                    created_areas += conn.execute(
                        "SELECT count(*) AS count FROM map_change_areas WHERE observation_id = %s",
                        (observation_id,),
                    ).fetchone()["count"]
                    previous = str(snapshot["id"])
                conn.commit()
        return {"observations": created_observations, "areas": created_areas}

    def rebuild_change_derivatives(self) -> dict[str, int]:
        """Transactionally rebuild v2 changes while preserving observations."""
        areas = 0
        features = 0
        with self.database.connect(dict_rows=True) as conn:
            source_rows = conn.execute("SELECT id FROM map_sources ORDER BY id").fetchall()
            for source in source_rows:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"wardotfun:ingest:{source['id']}",),
                )
            observations = conn.execute(
                """
                SELECT id, snapshot_id, previous_snapshot_id, is_baseline
                FROM map_snapshot_observations
                ORDER BY source_id, observed_at, id
                """
            ).fetchall()
            for table, trigger in (
                ("map_change_area_features", "map_change_area_features_immutable"),
                ("map_change_areas", "map_change_areas_immutable"),
                ("map_change_features", "map_change_features_immutable"),
                ("map_snapshot_observations", "map_snapshot_observations_immutable"),
            ):
                conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            conn.execute("DELETE FROM map_change_area_features")
            conn.execute("DELETE FROM map_change_areas")
            conn.execute("DELETE FROM map_change_features")
            for observation in observations:
                observation_id = str(observation["id"])
                old_snapshot = observation["previous_snapshot_id"]
                new_snapshot = observation["snapshot_id"]
                if observation["is_baseline"] or old_snapshot is None:
                    stats = {
                        "added_count": 0, "removed_count": 0,
                        "modified_count": 0, "style_count": 0, "bounds": None,
                    }
                else:
                    stats = conn.execute(
                        """
                        WITH feature_stats AS (
                            SELECT count(*) FILTER (WHERE change_type = 'added')::integer AS added_count,
                                   count(*) FILTER (WHERE change_type = 'removed')::integer AS removed_count,
                                   count(*) FILTER (WHERE change_type = 'modified')::integer AS modified_count,
                                   ST_Envelope(ST_Collect(bounds)) AS bounds
                            FROM map_snapshot_feature_diff(%s, %s)
                        ), old_layers AS (
                            SELECT layer_key, label, geometry_type, paint
                            FROM map_layer_versions WHERE snapshot_id = %s
                        ), new_layers AS (
                            SELECT layer_key, label, geometry_type, paint
                            FROM map_layer_versions WHERE snapshot_id = %s
                        ), style_stats AS (
                            SELECT count(*)::integer AS style_count
                            FROM old_layers o FULL JOIN new_layers n USING(layer_key)
                            WHERE o.layer_key IS NULL OR n.layer_key IS NULL
                               OR o.label IS DISTINCT FROM n.label
                               OR o.geometry_type IS DISTINCT FROM n.geometry_type
                               OR o.paint IS DISTINCT FROM n.paint
                        )
                        SELECT feature_stats.added_count, feature_stats.removed_count,
                               feature_stats.modified_count, style_stats.style_count,
                               ST_AsGeoJSON(feature_stats.bounds) AS bounds
                        FROM feature_stats CROSS JOIN style_stats
                        """,
                        (old_snapshot, new_snapshot, old_snapshot, new_snapshot),
                    ).fetchone()
                conn.execute(
                    """
                    UPDATE map_snapshot_observations SET
                        added_count = %s, removed_count = %s, modified_count = %s,
                        style_count = %s,
                        bounds = CASE WHEN %s::text IS NULL THEN NULL
                          ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) END
                    WHERE id = %s
                    """,
                    (
                        stats["added_count"], stats["removed_count"],
                        stats["modified_count"], stats["style_count"],
                        stats["bounds"], stats["bounds"], observation_id,
                    ),
                )
                if observation["is_baseline"] or old_snapshot is None:
                    continue
                inserted = conn.execute(
                    """
                    INSERT INTO map_change_features(
                        observation_id, logical_key, change_type,
                        old_feature_version_id, new_feature_version_id,
                        old_layer_key, new_layer_key, identity_confidence, bounds
                    )
                    SELECT %s, logical_key, change_type, old_feature_version_id,
                           new_feature_version_id, old_layer_key, new_layer_key,
                           identity_confidence, bounds
                    FROM map_snapshot_feature_diff(%s, %s)
                    """,
                    (observation_id, old_snapshot, new_snapshot),
                ).rowcount
                features += max(inserted, 0)
                self._create_change_areas(conn, observation_id, stats["style_count"])
                areas += conn.execute(
                    "SELECT count(*) AS count FROM map_change_areas WHERE observation_id = %s",
                    (observation_id,),
                ).fetchone()["count"]
            for table, trigger in reversed((
                ("map_change_area_features", "map_change_area_features_immutable"),
                ("map_change_areas", "map_change_areas_immutable"),
                ("map_change_features", "map_change_features_immutable"),
                ("map_snapshot_observations", "map_snapshot_observations_immutable"),
            )):
                conn.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
            conn.commit()
        return {"observations": len(observations), "features": features, "areas": areas}

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

                latest_observation = conn.execute(
                    """
                    SELECT id, snapshot_id FROM map_snapshot_observations
                    WHERE source_id = %s
                    ORDER BY observed_at DESC, id DESC LIMIT 1
                    """,
                    (source_id,),
                ).fetchone()
                previous_snapshot_id = (
                    str(latest_observation["snapshot_id"]) if latest_observation else None
                )
                existing = conn.execute(
                    "SELECT id FROM map_snapshots WHERE source_id = %s AND content_hash = %s",
                    (source_id, digest),
                ).fetchone()
                if existing and str(existing["id"]) == previous_snapshot_id:
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

                if existing:
                    snapshot_id = str(existing["id"])
                    observation_id = self._create_observation(
                        conn,
                        source_id=source_id,
                        snapshot_id=snapshot_id,
                        previous_snapshot_id=previous_snapshot_id,
                        observed_at=captured_at,
                        ingest_run_id=run_id,
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
                    return IngestResult(
                        run_id, "stored", snapshot_id, digest, count, observation_id
                    )

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
                observation_id = self._create_observation(
                    conn,
                    source_id=source_id,
                    snapshot_id=snapshot_id,
                    previous_snapshot_id=previous_snapshot_id,
                    observed_at=captured_at,
                    ingest_run_id=run_id,
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
            return IngestResult(
                run_id, "stored", snapshot_id, digest, count, observation_id
            )
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
                "SELECT min(calendar_date) AS value FROM map_snapshot_observations"
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
                       snap.id AS snapshot_id, observation.observed_at AS captured_at,
                       observation.calendar_date AS snapshot_date,
                       snap.feature_count, snap.content_hash,
                       latest_run.status AS ingest_status,
                       latest_run.finished_at AS ingest_finished_at,
                       latest_run.error AS ingest_error
                FROM map_sources s
                LEFT JOIN LATERAL (
                    SELECT observed.* FROM map_snapshot_observations observed
                    WHERE observed.source_id = s.id
                      AND observed.observed_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Europe/Kyiv')
                    ORDER BY observed.observed_at DESC, observed.id DESC LIMIT 1
                ) observation ON true
                LEFT JOIN map_snapshots snap ON snap.id = observation.snapshot_id
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

    @staticmethod
    def _change_item(row) -> dict[str, Any]:
        return {
            "id": str(row["area_id"]),
            "observation_id": str(row["observation_id"]),
            "source": {
                "id": row["source_id"], "kind": row["kind"],
                "display_name": row["display_name"],
            },
            "observed_at": row["observed_at"].isoformat(),
            "date": row["calendar_date"].strftime("%Y%m%d"),
            "counts": {
                "added": row["added_count"], "removed": row["removed_count"],
                "modified": row["modified_count"], "style": row["style_count"],
            },
            "bounds": [row["west"], row["south"], row["east"], row["north"]],
            "thumbnail_url": f"/api/map-change-images/v2/{row['area_id']}.svg",
            "detail_url": f"/api/map-changes/{row['area_id']}",
            "cursor": encode_change_cursor(row["observed_at"], str(row["area_id"])),
        }

    def get_map_changes(
        self,
        *,
        selected: str | None = None,
        source_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50:
            raise TemporalDataError("change-feed limit must be between 1 and 50")
        if source_id and not SOURCE_RE.fullmatch(source_id):
            raise TemporalDataError("invalid change-feed source")
        selected_date = parse_compact_date(selected, default=datetime.now(KYIV).date())
        if selected_date > datetime.now(KYIV).date():
            raise TemporalDataError("change-feed date cannot be in the future")
        cutoff = datetime.combine(selected_date + timedelta(days=1), datetime.min.time(), KYIV)
        decoded = decode_change_cursor(cursor)
        cursor_time, cursor_id = decoded if decoded else (None, None)
        with self.database.connect(dict_rows=True) as conn:
            rows = conn.execute(
                """
                SELECT area.id AS area_id, observation.id AS observation_id,
                       observation.observed_at, observation.calendar_date,
                       source.id AS source_id, source.kind, source.display_name,
                       area.added_count, area.removed_count, area.modified_count,
                       area.style_count,
                       ST_XMin(box3d(area.bounds)) AS west, ST_YMin(box3d(area.bounds)) AS south,
                       ST_XMax(box3d(area.bounds)) AS east, ST_YMax(box3d(area.bounds)) AS north
                FROM map_change_areas area
                JOIN map_snapshot_observations observation ON observation.id = area.observation_id
                JOIN map_sources source ON source.id = observation.source_id
                WHERE observation.observed_at < %s
                  AND source.enabled
                  AND (%s::text IS NULL OR source.id = %s)
                  AND (%s::timestamptz IS NULL OR
                       (observation.observed_at, area.id) < (%s::timestamptz, %s::uuid))
                ORDER BY observation.observed_at DESC, area.id DESC
                LIMIT %s
                """,
                (
                    cutoff, source_id, source_id, cursor_time, cursor_time,
                    cursor_id, limit + 1,
                ),
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [self._change_item(row) for row in rows]
        return {
            "date": selected_date.strftime("%Y%m%d"),
            "timezone": "Europe/Kyiv",
            "items": items,
            "next_cursor": items[-1]["cursor"] if has_more and items else None,
        }

    def get_map_change_status(
        self, *, selected: str | None = None, after: str | None = None
    ) -> dict[str, Any]:
        selected_date = parse_compact_date(selected, default=datetime.now(KYIV).date())
        if selected_date > datetime.now(KYIV).date():
            raise TemporalDataError("change-feed date cannot be in the future")
        cutoff = datetime.combine(selected_date + timedelta(days=1), datetime.min.time(), KYIV)
        decoded = decode_change_cursor(after)
        after_time, after_id = decoded if decoded else (None, None)
        with self.database.connect(dict_rows=True) as conn:
            latest = conn.execute(
                """
                SELECT observation.observed_at, area.id
                FROM map_change_areas area
                JOIN map_snapshot_observations observation ON observation.id = area.observation_id
                JOIN map_sources source ON source.id = observation.source_id
                WHERE observation.observed_at < %s AND source.enabled
                ORDER BY observation.observed_at DESC, area.id DESC LIMIT 1
                """,
                (cutoff,),
            ).fetchone()
            if decoded:
                unread = conn.execute(
                    """
                    SELECT LEAST(count(*), 100)::integer AS count
                    FROM map_change_areas area
                    JOIN map_snapshot_observations observation ON observation.id = area.observation_id
                    JOIN map_sources source ON source.id = observation.source_id
                    WHERE observation.observed_at < %s AND source.enabled
                      AND (observation.observed_at, area.id) > (%s, %s::uuid)
                    """,
                    (cutoff, after_time, after_id),
                ).fetchone()["count"]
            else:
                unread = 0
        latest_cursor = (
            encode_change_cursor(latest["observed_at"], str(latest["id"])) if latest else None
        )
        return {
            "date": selected_date.strftime("%Y%m%d"),
            "latest_cursor": latest_cursor,
            "unread_count": unread,
        }

    @staticmethod
    def _snapshot_descriptor(conn, source_id: str, snapshot_id) -> dict[str, Any] | None:
        if not snapshot_id:
            return None
        snapshot_id = str(snapshot_id)
        snapshot = conn.execute(
            "SELECT feature_count, content_hash FROM map_snapshots WHERE id = %s",
            (snapshot_id,),
        ).fetchone()
        layers = conn.execute(
            """
            SELECT layer_key, label, geometry_type, paint, feature_count
            FROM map_layer_versions WHERE snapshot_id = %s ORDER BY ordinal
            """,
            (snapshot_id,),
        ).fetchall()
        return {
            "id": snapshot_id,
            "feature_count": snapshot["feature_count"],
            "content_hash": snapshot["content_hash"],
            "tile_url": f"/api/map-tiles/{source_id}/{snapshot_id}/{{z}}/{{x}}/{{y}}.pbf",
            "layers": [{
                "id": layer["layer_key"], "label": layer["label"],
                "geom_type": layer["geometry_type"], "paint": layer["paint"],
                "feature_count": layer["feature_count"], "source_layer": "features",
            } for layer in layers],
        }

    def get_map_change(self, area_id: str) -> dict[str, Any]:
        try:
            area_id = str(uuid.UUID(area_id))
        except (ValueError, TypeError) as exc:
            raise TemporalDataError("invalid map-change area") from exc
        with self.database.connect(dict_rows=True) as conn:
            row = conn.execute(
                """
                SELECT area.id AS area_id, observation.id AS observation_id,
                       observation.source_id, observation.snapshot_id,
                       observation.previous_snapshot_id, observation.observed_at,
                       observation.calendar_date, source.kind, source.display_name,
                       area.added_count, area.removed_count, area.modified_count,
                       area.style_count,
                       ST_XMin(box3d(area.bounds)) AS west, ST_YMin(box3d(area.bounds)) AS south,
                       ST_XMax(box3d(area.bounds)) AS east, ST_YMax(box3d(area.bounds)) AS north
                FROM map_change_areas area
                JOIN map_snapshot_observations observation ON observation.id = area.observation_id
                JOIN map_sources source ON source.id = observation.source_id
                WHERE area.id = %s
                """,
                (area_id,),
            ).fetchone()
            if not row:
                raise KeyError("map-change area not found")
            before = self._snapshot_descriptor(conn, row["source_id"], row["previous_snapshot_id"])
            after = self._snapshot_descriptor(conn, row["source_id"], row["snapshot_id"])
        item = self._change_item(row)
        item.update({
            "before": before,
            "after": after,
            "change_tile_url": f"/api/map-change-tiles/v2/{area_id}/{{z}}/{{x}}/{{y}}.pbf",
        })
        return item

    def get_change_svg(self, area_id: str) -> tuple[bytes, str]:
        detail = self.get_map_change(area_id)
        west, south, east, north = detail["bounds"]
        with self.database.connect(dict_rows=True) as conn:
            rows = conn.execute(
                """
                WITH area AS (
                    SELECT change_area.id, change_area.bounds, observation.snapshot_id,
                           change_area.observation_id
                    FROM map_change_areas change_area
                    JOIN map_snapshot_observations observation
                      ON observation.id = change_area.observation_id
                    WHERE change_area.id = %s
                ),
                geometries AS (
                    SELECT delta.change_type, delta.phase, delta.geometry
                    FROM area
                    CROSS JOIN LATERAL map_change_area_geometries(area.id) delta
                    UNION ALL
                    SELECT 'context'::text, 'after'::text, context.geometry
                    FROM area
                    JOIN LATERAL (
                        SELECT fv.geometry
                        FROM snapshot_features sf
                        JOIN feature_versions fv ON fv.id = sf.feature_version_id
                        WHERE sf.snapshot_id = area.snapshot_id
                          AND fv.geometry && ST_Expand(area.bounds, 0.25)
                          AND NOT EXISTS (
                              SELECT 1 FROM map_change_features changed
                              WHERE changed.observation_id = area.observation_id
                                AND fv.id IN (changed.old_feature_version_id, changed.new_feature_version_id)
                          )
                        ORDER BY fv.id LIMIT 2000
                    ) context ON true
                )
                SELECT change_type, phase, ST_AsGeoJSON(geometry) AS geometry
                FROM geometries
                ORDER BY change_type, phase, md5(ST_AsEWKB(geometry))
                """,
                (area_id,),
            ).fetchall()
        min_x, min_y = _mercator_xy(west, south)
        max_x, max_y = _mercator_xy(east, north)
        span = max(max_x - min_x, max_y - min_y, 10000.0)
        min_x -= span * 0.12; max_x += span * 0.12
        min_y -= span * 0.12; max_y += span * 0.12
        width, height = max_x - min_x, max_y - min_y
        scale = min(600 / width, 320 / height)
        offset_x = (640 - width * scale) / 2
        offset_y = (360 - height * scale) / 2

        def project(point):
            x, y = _mercator_xy(float(point[0]), float(point[1]))
            return offset_x + (x - min_x) * scale, 360 - (offset_y + (y - min_y) * scale)

        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            groups[(row["change_type"], row["phase"])].append(
                _svg_geometry(json.loads(row["geometry"]), project)
            )
        styles = {
            ("context", "after"): "fill:#68717a;fill-opacity:.06;stroke:#68717a;stroke-opacity:.3;stroke-width:1",
            ("added", "after"): "fill:#35c46a;fill-opacity:.35;stroke:#54e383;stroke-width:3",
            ("removed", "before"): "fill:#e34b4b;fill-opacity:.22;stroke:#ff6868;stroke-width:3;stroke-dasharray:8 5",
            ("modified", "before"): "fill:none;stroke:#e34b4b;stroke-opacity:.55;stroke-width:2;stroke-dasharray:6 5",
            ("modified", "after"): "fill:#35c46a;fill-opacity:.35;stroke:#54e383;stroke-width:3",
            ("modified", "style"): "fill:#d5a93d;fill-opacity:.28;stroke:#f0c75e;stroke-width:3",
        }
        layers = "".join(
            f'<g style="{styles.get(key, "fill:none;stroke:#aaa")}">{"".join(values)}</g>'
            for key, values in groups.items()
        )
        if detail["counts"]["style"]:
            layers += '<rect x="120" y="72" width="400" height="216" rx="8" fill="none" stroke="#f0c75e" stroke-width="4"/><text x="320" y="188" text-anchor="middle" fill="#f0c75e" font-family="monospace" font-size="20">STYLE CHANGE</text>'
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360">'
            '<rect width="640" height="360" fill="#111418"/>'
            '<path d="M0 90H640M0 180H640M0 270H640M160 0V360M320 0V360M480 0V360" stroke="#293038" stroke-width="1"/>'
            f'{layers}</svg>'
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(b"change-svg-v2:" + svg).hexdigest() + '"'
        return svg, etag

    def get_change_tile(self, area_id: str, z: int, x: int, y: int) -> tuple[bytes, str]:
        try:
            area_id = str(uuid.UUID(area_id))
        except (ValueError, TypeError) as exc:
            raise TemporalDataError("invalid map-change area") from exc
        if not 0 <= z <= 22:
            raise TemporalDataError("zoom must be between 0 and 22")
        limit = 1 << z
        if not (0 <= x < limit and 0 <= y < limit):
            raise TemporalDataError("tile coordinate is outside the zoom grid")
        with self.database.connect() as conn:
            exists = conn.execute("SELECT 1 FROM map_change_areas WHERE id = %s", (area_id,)).fetchone()
            if not exists:
                raise KeyError("map-change area not found")
            row = conn.execute(
                """
                WITH tile_bounds AS (SELECT ST_TileEnvelope(%s, %s, %s) AS geom),
                area AS (SELECT id, observation_id FROM map_change_areas WHERE id = %s),
                change_geometries AS (
                    SELECT delta.logical_key, delta.change_type, delta.phase, delta.geometry
                    FROM area
                    CROSS JOIN LATERAL map_change_area_geometries(area.id) delta
                ), tile_rows AS (
                    SELECT logical_key, change_type, phase,
                           ST_AsMVTGeom(ST_Transform(geometry, 3857), tile_bounds.geom,
                                        %s, %s, true) AS geom
                    FROM change_geometries CROSS JOIN tile_bounds
                    WHERE geometry && ST_Transform(tile_bounds.geom, 4326)
                )
                SELECT ST_AsMVT(tile_rows, 'changes', %s, 'geom')
                FROM tile_rows WHERE geom IS NOT NULL
                """,
                (z, x, y, area_id, MVT_EXTENT, MVT_BUFFER, MVT_EXTENT),
            ).fetchone()
        tile = bytes(row[0]) if row and row[0] is not None else b""
        etag = '"' + hashlib.sha256(
            f"change-mvt-v2:{area_id}:{z}:{x}:{y}".encode("ascii")
        ).hexdigest() + '"'
        return tile, etag

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
