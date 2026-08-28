import math


def mercator_to_lonlat(x: float, y: float) -> tuple[float, float]:
    """Convert Web Mercator (EPSG:3857) coordinates to WGS84 (lon, lat)."""
    R = 6378137.0
    lon = (x / R) * (180 / math.pi)
    lat = (2 * math.atan(math.exp(y / R)) - math.pi / 2) * (180 / math.pi)
    return lon, lat


def rings_to_geojson_coords(rings: list) -> list:
    """Convert ArcGIS EPSG:3857 rings to WGS84 GeoJSON coordinate arrays."""
    return [
        [list(mercator_to_lonlat(x, y)) for x, y in ring]
        for ring in rings
    ]


def arcgis_features_to_geojson(features: list) -> dict:
    """Convert a list of ArcGIS features to a GeoJSON FeatureCollection."""
    geojson_features = []
    for feature in features:
        geometry = feature.get("geometry")
        if not geometry or not geometry.get("rings"):
            continue
        geojson_features.append({
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": rings_to_geojson_coords(geometry["rings"]),
            },
            "properties": feature.get("attributes") or {},
        })
    return {
        "type": "FeatureCollection",
        "features": geojson_features,
    }


def city_geometry_to_geojson(geometry: dict) -> dict | None:
    """Convert a city's ArcGIS geometry (rings in EPSG:3857) to a GeoJSON Polygon."""
    if not geometry or not geometry.get("rings"):
        return None
    return {
        "type": "Polygon",
        "coordinates": rings_to_geojson_coords(geometry["rings"]),
    }


def city_geometry_to_marker(geometry: dict) -> dict | None:
    """Return an area-weighted representative point for a city polygon."""
    if not geometry or not geometry.get("rings"):
        return None

    rings = rings_to_geojson_coords(geometry["rings"])
    ring = max(rings, key=lambda coords: abs(_ring_signed_area(coords)), default=[])
    if not ring:
        return None

    area = _ring_signed_area(ring)
    if abs(area) < 1e-12:
        lon = (min(point[0] for point in ring) + max(point[0] for point in ring)) / 2
        lat = (min(point[1] for point in ring) + max(point[1] for point in ring)) / 2
    else:
        lon_sum = 0.0
        lat_sum = 0.0
        for current, following in zip(ring, ring[1:] + ring[:1]):
            cross = current[0] * following[1] - following[0] * current[1]
            lon_sum += (current[0] + following[0]) * cross
            lat_sum += (current[1] + following[1]) * cross
        lon = lon_sum / (6 * area)
        lat = lat_sum / (6 * area)

    return {"type": "Point", "coordinates": [lon, lat]}


def _ring_signed_area(ring: list) -> float:
    area_twice = 0.0
    for current, following in zip(ring, ring[1:] + ring[:1]):
        area_twice += current[0] * following[1] - following[0] * current[1]
    return area_twice / 2
