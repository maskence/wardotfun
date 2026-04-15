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
