import json
import logging
from pathlib import Path

from geo_utils import city_geometry_to_geojson, city_geometry_to_marker

logger = logging.getLogger(__name__)

DATA_PATH = Path(__file__).parent / "data" / "city_market_map.json"

_data: dict = {"cities": {}}


def load() -> dict:
    global _data
    _data = _load_from_disk()
    return _data


def reload() -> dict:
    logger.info("city_map: reloading from disk")
    return load()


def get() -> dict:
    return _data


def _load_from_disk() -> dict:
    if not DATA_PATH.exists():
        logger.warning("city_market_map.json not found at %s — serving empty map", DATA_PATH)
        return {"cities": {}}

    try:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to load city_market_map.json: %s", exc)
        return {"cities": {}}

    cities = raw.get("cities") or {}
    converted = {}
    for city_id, city_entry in cities.items():
        entry = dict(city_entry)
        raw_geometry = entry.get("geometry")
        if raw_geometry:
            entry["geometry"] = city_geometry_to_geojson(raw_geometry)
            entry["marker"] = city_geometry_to_marker(raw_geometry)
        converted[city_id] = entry

    logger.info("city_map: loaded %d cities", len(converted))
    return {**raw, "cities": converted}
