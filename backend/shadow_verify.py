"""Compare current legacy mapper caches with the latest PostGIS snapshots."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from .database import PostGISDatabase
    from .mapper_service import MapperService
    from .temporal_repository import content_hash, geometry_bounds, normalized_overlay
except ImportError:
    from database import PostGISDatabase
    from mapper_service import MapperService
    from temporal_repository import content_hash, geometry_bounds, normalized_overlay


def _bounds_tuple(bounds):
    if not bounds:
        return None
    points = bounds["coordinates"][0]
    return (
        min(point[0] for point in points),
        min(point[1] for point in points),
        max(point[0] for point in points),
        max(point[1] for point in points),
    )


def verify_shadow(database=None, mapper_service=None) -> dict:
    database = database or PostGISDatabase()
    mapper_service = mapper_service or MapperService()
    mapper_service.load_caches()
    results = []
    with database.connect(dict_rows=True) as conn:
        for _kind, source in mapper_service.iter_sources():
            payload = source.get_overlay()
            material = normalized_overlay(payload)
            expected_layers = {
                layer["id"]: {
                    "geometry_type": layer["geom_type"],
                    "paint": layer["paint"],
                    "feature_count": len(layer["features"]),
                }
                for layer in material["layers"]
            }
            expected_geometries = [
                feature["geometry"]
                for layer in material["layers"]
                for feature in layer["features"]
            ]
            expected = {
                "hash": content_hash(material),
                "feature_count": sum(item["feature_count"] for item in expected_layers.values()),
                "layers": expected_layers,
                "bounds": _bounds_tuple(geometry_bounds(expected_geometries)),
            }
            snapshot = conn.execute(
                """
                SELECT id, content_hash, feature_count,
                       ST_XMin(box3d(bounds)) AS west,
                       ST_YMin(box3d(bounds)) AS south,
                       ST_XMax(box3d(bounds)) AS east,
                       ST_YMax(box3d(bounds)) AS north
                FROM map_snapshots WHERE source_id = %s
                ORDER BY captured_at DESC LIMIT 1
                """,
                (source.id,),
            ).fetchone()
            stored_layers = {}
            if snapshot:
                stored_layers = {
                    row["layer_key"]: {
                        "geometry_type": row["geometry_type"],
                        "paint": row["paint"],
                        "feature_count": row["feature_count"],
                    }
                    for row in conn.execute(
                        """
                        SELECT layer_key, geometry_type, paint, feature_count
                        FROM map_layer_versions WHERE snapshot_id = %s
                        ORDER BY ordinal
                        """,
                        (snapshot["id"],),
                    )
                }
            actual = None if not snapshot else {
                "snapshot_id": str(snapshot["id"]),
                "hash": snapshot["content_hash"],
                "feature_count": snapshot["feature_count"],
                "layers": stored_layers,
                "bounds": (
                    snapshot["west"], snapshot["south"],
                    snapshot["east"], snapshot["north"],
                ) if snapshot["west"] is not None else None,
            }
            mismatches = []
            if not actual:
                mismatches.append("snapshot_missing")
            else:
                for key in ("hash", "feature_count", "layers", "bounds"):
                    if actual[key] != expected[key]:
                        mismatches.append(key)
            results.append({
                "source_id": source.id,
                "ok": not mismatches,
                "mismatches": mismatches,
                "expected": expected,
                "actual": actual,
            })
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "ok": all(item["ok"] for item in results),
        "sources": results,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", type=Path, help="append each report to this JSONL file")
    parser.add_argument("--no-fail", action="store_true", help="report mismatches without a failing exit status")
    args = parser.parse_args(argv)
    report = verify_shadow()
    encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    print(encoded)
    if args.jsonl:
        args.jsonl.parent.mkdir(parents=True, exist_ok=True)
        with args.jsonl.open("a", encoding="utf-8") as target:
            target.write(encoded + "\n")
    if not report["ok"] and not args.no_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
