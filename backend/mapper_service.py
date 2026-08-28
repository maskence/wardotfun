import logging
import pickle
import re
import threading
import time
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen
import json

from geo_utils import arcgis_features_to_geojson

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent / "data" / "mapper_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}


def _request_bytes(url: str, if_modified_since: str | None = None) -> tuple[bytes | None, str | None]:
    headers = {"User-Agent": "Mozilla/5.0"}
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            last_modified = resp.headers.get("Last-Modified")
            return resp.read(), last_modified
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, None
        raise


def _request_json(url: str, if_modified_since: str | None = None) -> tuple[dict | None, str | None]:
    body, last_modified = _request_bytes(url, if_modified_since)
    if body is None:
        return None, None
    return json.loads(body), last_modified


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _kml_color_to_hex_opacity(raw: str | None) -> tuple[str | None, float | None]:
    if not raw:
        return None, None
    color = raw.strip().lower()
    if len(color) != 8:
        return None, None
    alpha = int(color[0:2], 16) / 255
    blue = int(color[2:4], 16)
    green = int(color[4:6], 16)
    red = int(color[6:8], 16)
    return f"#{red:02x}{green:02x}{blue:02x}", round(alpha, 3)


def _parse_kml_coordinates(raw: str | None) -> list[list[float]]:
    if not raw:
        return []
    coords = []
    for token in raw.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coords.append([lon, lat])
    return coords


def _polygon_from_kml(node: ET.Element) -> dict | None:
    outer = node.find("./k:outerBoundaryIs/k:LinearRing/k:coordinates", KML_NS)
    if outer is None:
        return None
    rings = []
    outer_coords = _parse_kml_coordinates(outer.text)
    if not outer_coords:
        return None
    rings.append(outer_coords)
    for inner in node.findall("./k:innerBoundaryIs/k:LinearRing/k:coordinates", KML_NS):
        inner_coords = _parse_kml_coordinates(inner.text)
        if inner_coords:
            rings.append(inner_coords)
    return {"type": "Polygon", "coordinates": rings}


def _line_from_kml(node: ET.Element) -> dict | None:
    coords = _parse_kml_coordinates(node.findtext("./k:coordinates", default="", namespaces=KML_NS))
    if len(coords) < 2:
        return None
    return {"type": "LineString", "coordinates": coords}


def _point_from_kml(node: ET.Element) -> dict | None:
    coords = _parse_kml_coordinates(node.findtext("./k:coordinates", default="", namespaces=KML_NS))
    if not coords:
        return None
    return {"type": "Point", "coordinates": coords[0]}


def _first_present(values: list):
    for value in values:
        if value is not None:
            return value
    return None


class BaseMapperSource:
    id: str
    display_name: str
    source_url: str
    attribution: str
    refresh_interval: int = 300

    def __init__(self):
        self._lock = threading.Lock()
        self._last_updated: float | None = None
        self._last_error: str | None = None

    def load_cache(self):
        raise NotImplementedError

    def refresh_if_due(self):
        raise NotImplementedError

    def get_summary(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source_url": self.source_url,
            "attribution": self.attribution,
            "last_updated": self._last_updated,
            "status": self._status(),
        }

    def get_overlay(self) -> dict:
        raise NotImplementedError

    def _status(self) -> str:
        raise NotImplementedError


class ISWMapperSource(BaseMapperSource):
    id = "isw"
    display_name = "ISW"
    source_url = "https://storymaps.arcgis.com/stories/36a7f6a6f5a9448496de641cf64bd375"
    attribution = "Institute for the Study of War"
    refresh_interval = 30

    LAYERS = {
        "control": (
            "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
            "VIEW_RussiaCoTinUkraine_V3/FeatureServer/49"
        ),
        "infiltration": (
            "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
            "View_AssessedRussianInfiltrationAreasinUkraine_V4/FeatureServer/0"
        ),
        "gains": (
            "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
            "Assessed_Russian_Gains_in_the_Past_24_Hours_view/FeatureServer/0"
        ),
        "advances": (
            "https://services5.arcgis.com/SaBe5HMtmnbqSWlu/arcgis/rest/services/"
            "AssessedRussianAdvanceInUkraine_V2_view/FeatureServer/0"
        ),
    }

    LAYER_TTL = {
        "control": 24 * 60 * 60,
    }

    LAYER_META = {
        "control": {
            "label": "Control",
            "geom_type": "polygon",
            "paint": {
                "fill_color": "#8b1a1a",
                "fill_opacity": 0.3,
                "line_color": "#8b1a1a",
                "line_width": 1.5,
                "line_opacity": 1.0,
            },
        },
        "infiltration": {
            "label": "Infiltration",
            "geom_type": "polygon",
            "paint": {
                "fill_color": "#d4b96a",
                "fill_opacity": 0.35,
                "line_color": "#b59f3b",
                "line_width": 1.5,
                "line_opacity": 1.0,
            },
        },
        "gains": {
            "label": "Russian Gains",
            "geom_type": "polygon",
            "paint": {
                "fill_color": "#e05555",
                "fill_opacity": 0.35,
                "line_color": "#cc3333",
                "line_width": 1.5,
                "line_opacity": 1.0,
            },
        },
        "advances": {
            "label": "Russian Advances",
            "geom_type": "polygon",
            "paint": {
                "fill_color": "#8b1a1a",
                "fill_opacity": 0.3,
                "line_color": "#8b1a1a",
                "line_width": 1.5,
                "line_opacity": 1.0,
            },
        },
    }

    def __init__(self):
        super().__init__()
        self._cache_path = CACHE_DIR / "isw.pkl"
        self._layers: dict[str, dict] = {
            name: {"type": "FeatureCollection", "features": []}
            for name in self.LAYERS
        }
        self._last_fetched: dict[str, float] = {}
        self._last_modified: dict[str, str] = {}

    def load_cache(self):
        if not self._cache_path.exists():
            return
        try:
            payload = pickle.loads(self._cache_path.read_bytes())
            with self._lock:
                self._layers = payload.get("layers", self._layers)
                self._last_fetched = payload.get("last_fetched", {})
                self._last_modified = payload.get("last_modified", {})
                self._last_updated = payload.get("last_updated")
            logger.info("Loaded ISW mapper cache")
        except Exception as exc:
            logger.warning("Failed to load ISW mapper cache: %s", exc)

    def refresh_if_due(self):
        changed = False
        last_error = None
        for name, url in self.LAYERS.items():
            if not self._should_fetch(name):
                continue
            try:
                lm_sent = self._last_modified.get(name)
                fc, last_modified = self._fetch_layer(name, url, lm_sent)
                fetched_at = time.time()
                if fc is None:
                    with self._lock:
                        self._last_fetched[name] = fetched_at
                    logger.info("ISW layer %s not modified (304)", name)
                    changed = True
                    continue
                with self._lock:
                    self._layers[name] = fc
                    self._last_fetched[name] = fetched_at
                    if last_modified:
                        self._last_modified[name] = last_modified
                    self._last_updated = fetched_at
                    self._last_error = None
                changed = True
                logger.info("ISW layer %s refreshed (%d features)", name, len(fc["features"]))
            except Exception as exc:
                last_error = str(exc)
                logger.warning("Failed to refresh ISW layer %s: %s", name, exc)
        if last_error:
            with self._lock:
                self._last_error = last_error
        if changed:
            self._save_cache()

    def get_overlay(self) -> dict:
        with self._lock:
            layers = [
                {
                    "id": name,
                    "label": meta["label"],
                    "geom_type": meta["geom_type"],
                    "paint": meta["paint"],
                    "data": self._layers.get(name, {"type": "FeatureCollection", "features": []}),
                }
                for name, meta in self.LAYER_META.items()
            ]
            return {
                "mapper_id": self.id,
                "display_name": self.display_name,
                "source_url": self.source_url,
                "attribution": self.attribution,
                "last_updated": self._last_updated,
                "status": self._status(),
                "layers": layers,
            }

    def _status(self) -> str:
        has_data = any(layer.get("features") for layer in self._layers.values())
        if self._last_error and has_data:
            return "stale"
        if self._last_error:
            return "error"
        return "ok" if has_data else "error"

    def _should_fetch(self, name: str) -> bool:
        ttl = self.LAYER_TTL.get(name, self.refresh_interval)
        last = self._last_fetched.get(name, 0)
        return (time.time() - last) >= ttl

    def _save_cache(self):
        payload = {
            "layers": self._layers,
            "last_fetched": self._last_fetched,
            "last_modified": self._last_modified,
            "last_updated": self._last_updated,
        }
        try:
            self._cache_path.write_bytes(pickle.dumps(payload))
        except Exception as exc:
            logger.warning("Failed to save ISW mapper cache: %s", exc)

    def _fetch_layer(self, name: str, url: str, if_modified_since: str | None = None) -> tuple[dict | None, str | None]:
        query_url = (
            f"{url}/query?where=1%3D1&outFields=*&returnGeometry=true"
            "&cacheHint=true&f=json"
        )
        data, last_modified = _request_json(query_url, if_modified_since)
        if data is None:
            return None, None
        error = data.get("error") if isinstance(data, dict) else None
        if error:
            raise RuntimeError(f"ArcGIS error for {name}: {error}")
        features = data.get("features") or []
        return arcgis_features_to_geojson(features), last_modified


class GoogleMyMapsMapperSource(BaseMapperSource):
    refresh_interval = 300

    def __init__(self, *, mapper_id: str, display_name: str, source_url: str, attribution: str,
                 kml_mid: str, included_folders: list[str] | None = None,
                 excluded_folders: list[str] | None = None, paint_overrides: dict | None = None):
        super().__init__()
        self.id = mapper_id
        self.display_name = display_name
        self.source_url = source_url
        self.attribution = attribution
        self._kml_url = f"https://www.google.com/maps/d/kml?forcekml=1&mid={kml_mid}"
        self._included_folders = included_folders
        self._excluded_folders = set(excluded_folders or [])
        self._paint_overrides = paint_overrides or {}
        self._cache_path = CACHE_DIR / f"{self.id}.pkl"
        self._payload: dict | None = None
        self._last_fetched: float | None = None
        self._kml_last_modified: str | None = None

    def load_cache(self):
        if not self._cache_path.exists():
            return
        try:
            payload = pickle.loads(self._cache_path.read_bytes())
            with self._lock:
                self._payload = payload.get("payload")
                self._last_fetched = payload.get("last_fetched")
                self._kml_last_modified = payload.get("kml_last_modified")
                self._last_updated = payload.get("last_updated")
            logger.info("Loaded %s mapper cache", self.id)
        except Exception as exc:
            logger.warning("Failed to load %s mapper cache: %s", self.id, exc)

    def refresh_if_due(self):
        if self._last_fetched and (time.time() - self._last_fetched) < self.refresh_interval:
            return
        try:
            lm_sent = self._kml_last_modified
            xml_bytes, last_modified = _request_bytes(self._kml_url, lm_sent)
            fetched_at = time.time()
            if xml_bytes is None:
                with self._lock:
                    self._last_fetched = fetched_at
                logger.info("%s mapper not modified (304)", self.id)
                self._save_cache()
                return
            payload = self._parse_kml(xml_bytes)
            with self._lock:
                self._payload = payload
                self._last_fetched = fetched_at
                if last_modified:
                    self._kml_last_modified = last_modified
                self._last_updated = fetched_at
                self._last_error = None
            self._save_cache()
            logger.info("%s mapper refreshed (%d layers)", self.id, len(payload["layers"]))
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
            logger.warning("Failed to refresh %s mapper: %s", self.id, exc)

    def get_overlay(self) -> dict:
        with self._lock:
            layers = self._payload["layers"] if self._payload else []
            return {
                "mapper_id": self.id,
                "display_name": self.display_name,
                "source_url": self.source_url,
                "attribution": self.attribution,
                "last_updated": self._last_updated,
                "status": self._status(),
                "layers": layers,
            }

    def _status(self) -> str:
        has_data = bool(self._payload and self._payload.get("layers"))
        if self._last_error and has_data:
            return "stale"
        if self._last_error:
            return "error"
        return "ok" if has_data else "error"

    def _save_cache(self):
        payload = {
            "payload": self._payload,
            "last_fetched": self._last_fetched,
            "kml_last_modified": self._kml_last_modified,
            "last_updated": self._last_updated,
        }
        try:
            self._cache_path.write_bytes(pickle.dumps(payload))
        except Exception as exc:
            logger.warning("Failed to save %s mapper cache: %s", self.id, exc)

    def _parse_kml(self, xml_bytes: bytes) -> dict:
        root = ET.fromstring(xml_bytes)
        styles = self._parse_styles(root)
        folders = root.findall("./k:Document/k:Folder", KML_NS)
        layers = []

        all_folder_names = [f.findtext("./k:name", default="", namespaces=KML_NS).strip() for f in folders]
        logger.info("%s KML folders: %s", self.id, all_folder_names)
        for folder in folders:
            name = folder.findtext("./k:name", default="", namespaces=KML_NS).strip()
            if self._included_folders is not None and name not in self._included_folders:
                continue
            if name in self._excluded_folders:
                continue

            features_by_type: dict[str, list[dict]] = {"polygon": [], "line": [], "point": []}
            for placemark in folder.findall(".//k:Placemark", KML_NS):
                placemark_features = self._placemark_features(placemark, styles)
                for feature in placemark_features:
                    geom_type = self._geojson_geom_type(feature["geometry"])
                    features_by_type[geom_type].append(feature)

            for geom_type in ["polygon", "line", "point"]:
                features = features_by_type[geom_type]
                if not features:
                    continue

                default_paint = self._default_paint(features[0]["properties"], geom_type)
                layer_id = _slugify(name if len([g for g in features_by_type.values() if g]) == 1 else f"{name} {geom_type}")
                layers.append({
                    "id": layer_id,
                    "label": name if len([g for g in features_by_type.values() if g]) == 1 else f"{name} ({geom_type})",
                    "geom_type": geom_type,
                    "paint": default_paint,
                    "data": {"type": "FeatureCollection", "features": features},
                })

        if not layers:
            raise RuntimeError(f"{self.display_name} KML parsed with no supported overlay layers")

        if self._paint_overrides:
            for layer in layers:
                layer["paint"].update(self._paint_overrides)

        return {"layers": layers}

    def _parse_styles(self, root: ET.Element) -> dict[str, dict]:
        base_styles = {}
        style_maps = {}

        for style in root.findall(".//k:Style", KML_NS):
            style_id = style.attrib.get("id")
            if not style_id:
                continue
            line_color, line_opacity = _kml_color_to_hex_opacity(style.findtext("./k:LineStyle/k:color", default="", namespaces=KML_NS))
            fill_color, fill_opacity = _kml_color_to_hex_opacity(style.findtext("./k:PolyStyle/k:color", default="", namespaces=KML_NS))
            icon_color, icon_opacity = _kml_color_to_hex_opacity(style.findtext("./k:IconStyle/k:color", default="", namespaces=KML_NS))
            width_text = style.findtext("./k:LineStyle/k:width", default="", namespaces=KML_NS)
            width = None
            try:
                width = float(width_text) if width_text else None
            except ValueError:
                width = None
            base_styles[f"#{style_id}"] = {
                "line_color": line_color,
                "line_opacity": line_opacity,
                "line_width": width,
                "fill_color": fill_color,
                "fill_opacity": fill_opacity,
                "circle_color": icon_color,
                "circle_opacity": icon_opacity,
            }

        for style_map in root.findall(".//k:StyleMap", KML_NS):
            style_map_id = style_map.attrib.get("id")
            if not style_map_id:
                continue
            normal_url = None
            for pair in style_map.findall("./k:Pair", KML_NS):
                if pair.findtext("./k:key", default="", namespaces=KML_NS) == "normal":
                    normal_url = pair.findtext("./k:styleUrl", default="", namespaces=KML_NS)
                    break
            if normal_url:
                style_maps[f"#{style_map_id}"] = normal_url

        resolved = dict(base_styles)
        for style_id, target in style_maps.items():
            resolved[style_id] = base_styles.get(target, {})
        return resolved

    def _placemark_features(self, placemark: ET.Element, styles: dict[str, dict]) -> list[dict]:
        style = styles.get(placemark.findtext("./k:styleUrl", default="", namespaces=KML_NS), {})
        base_props = {
            "name": placemark.findtext("./k:name", default="", namespaces=KML_NS),
            "description": placemark.findtext("./k:description", default="", namespaces=KML_NS),
            "fill_color": style.get("fill_color"),
            "fill_opacity": style.get("fill_opacity"),
            "line_color": _first_present([style.get("line_color"), style.get("fill_color")]),
            "line_opacity": _first_present([style.get("line_opacity"), style.get("fill_opacity"), 1.0]),
            "line_width": _first_present([style.get("line_width"), 1.5]),
            "circle_color": _first_present([style.get("circle_color"), style.get("fill_color"), style.get("line_color")]),
            "circle_opacity": _first_present([style.get("circle_opacity"), 1.0]),
        }

        features = []
        for polygon in placemark.findall(".//k:Polygon", KML_NS):
            geometry = _polygon_from_kml(polygon)
            if geometry:
                features.append({"type": "Feature", "geometry": geometry, "properties": dict(base_props)})
        for line in placemark.findall(".//k:LineString", KML_NS):
            geometry = _line_from_kml(line)
            if geometry:
                features.append({"type": "Feature", "geometry": geometry, "properties": dict(base_props)})
        for point in placemark.findall(".//k:Point", KML_NS):
            geometry = _point_from_kml(point)
            if geometry:
                features.append({"type": "Feature", "geometry": geometry, "properties": dict(base_props)})
        return features

    def _geojson_geom_type(self, geometry: dict) -> str:
        mapping = {
            "Polygon": "polygon",
            "MultiPolygon": "polygon",
            "LineString": "line",
            "MultiLineString": "line",
            "Point": "point",
            "MultiPoint": "point",
        }
        return mapping.get(geometry["type"], "polygon")

    def _default_paint(self, props: dict, geom_type: str) -> dict:
        if geom_type == "polygon":
            return {
                "fill_color": props.get("fill_color") or "#999999",
                "fill_opacity": props.get("fill_opacity") if props.get("fill_opacity") is not None else 0.35,
                "line_color": props.get("line_color") or "#999999",
                "line_opacity": props.get("line_opacity") if props.get("line_opacity") is not None else 1.0,
                "line_width": props.get("line_width") if props.get("line_width") is not None else 1.5,
            }
        if geom_type == "line":
            return {
                "line_color": props.get("line_color") or "#999999",
                "line_opacity": props.get("line_opacity") if props.get("line_opacity") is not None else 1.0,
                "line_width": props.get("line_width") if props.get("line_width") is not None else 1.5,
            }
        return {
            "circle_color": props.get("circle_color") or "#999999",
            "circle_opacity": props.get("circle_opacity") if props.get("circle_opacity") is not None else 1.0,
            "circle_radius": 4,
            "circle_stroke_color": "#ffffff",
            "circle_stroke_width": 1.5,
        }


class MapperService:
    def __init__(self):
        self._fortification = GoogleMyMapsMapperSource(
            mapper_id="weeb-union-forts",
            display_name="Weeb Union Fortifications",
            source_url="https://www.google.com/maps/d/viewer?mid=1sFRJhfNV-R1kSTXcej2MeHBzcCL8X0o",
            attribution="Weeb Union",
            kml_mid="1sFRJhfNV-R1kSTXcej2MeHBzcCL8X0o",
            included_folders=[
                "Fortifications Donetsk",
                "Fortifications Zaporizhzhia",
                "Anti Tank Ditches & Dragons Teeth",
            ],
            paint_overrides={
                "fill_color": "#aaaaaa",
                "fill_opacity": 0.3,
                "line_color": "#cccccc",
                "line_opacity": 0.85,
                "circle_color": "#cccccc",
                "circle_opacity": 0.85,
            },
        )
        self._fortification.refresh_interval = 7 * 24 * 60 * 60

        self._sources: dict[str, BaseMapperSource] = {
            "isw": ISWMapperSource(),
            "amk": GoogleMyMapsMapperSource(
                mapper_id="amk",
                display_name="AMK Mapping",
                source_url="https://www.google.com/maps/d/viewer?mid=1thW9kqnDOaS2lAepLhLdzSX8Ur9Sc4k",
                attribution="AMK Mapping / Google My Maps",
                kml_mid="1thW9kqnDOaS2lAepLhLdzSX8Ur9Sc4k",
                included_folders=[
                    "Confirmed Russian controlled territory",
                    "Confirmed Ukrainian control",
                    "Infiltration zone",
                    "Confirmed Russian advances",
                    "Confirmed Ukrainian advances",
                ],
            ),
            "suriak": GoogleMyMapsMapperSource(
                mapper_id="suriak",
                display_name="Suriak Maps",
                source_url="https://www.google.com/maps/d/viewer?mid=1V8NzjQkzMOhpuLhkktbiKgodOQ27X6IV",
                attribution="Suriak Maps / Google My Maps",
                kml_mid="1V8NzjQkzMOhpuLhkktbiKgodOQ27X6IV",
                included_folders=[
                    "Frontlines",
                ],
            ),
            "weeb-union": GoogleMyMapsMapperSource(
                mapper_id="weeb-union",
                display_name="Weeb Union",
                source_url="https://www.google.com/maps/d/viewer?mid=1sFRJhfNV-R1kSTXcej2MeHBzcCL8X0o",
                attribution="Weeb Union / Google My Maps",
                kml_mid="1sFRJhfNV-R1kSTXcej2MeHBzcCL8X0o",
                included_folders=[
                    "Frontlines",
                    "Recap April",
                    "Actions",
                ],
            ),
            "uacontrolmap": GoogleMyMapsMapperSource(
                mapper_id="uacontrolmap",
                display_name="UA Control Map",
                source_url="https://www.uacontrolmap.com/",
                attribution="UA Control Map / Google My Maps",
                kml_mid="1xPxgT8LtUjuspSOGHJc2VzA5O5jWMTE",
                included_folders=[
                    "Ukrainian advances",
                    "Confirmed Russian advances",
                    "Confirmed Control",
                    "Defense lines and forts",
                    "Ukrainian advance",
                ],
            ),
            "kalibrated": GoogleMyMapsMapperSource(
                mapper_id="kalibrated",
                display_name="Kalibrated Maps",
                source_url="https://www.google.com/maps/d/viewer?mid=1VcGZiwrEi8t9kXvVnWNEmO5ScvBWK6A",
                attribution="Kalibrated Maps / Google My Maps",
                kml_mid="1VcGZiwrEi8t9kXvVnWNEmO5ScvBWK6A",
                included_folders=[
                    "Ukrainian advances",
                    "Confirmed Russian advances",
                    "Kharkov/Kursk",
                    "Confirmed Control",
                    "Ukrainian advance",
                ],
            ),
        }
        self._thread: threading.Thread | None = None

    def get_fortification_overlay(self) -> dict:
        return self._fortification.get_overlay()

    def start(self):
        self._fortification.load_cache()
        for source in self._sources.values():
            source.load_cache()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("MapperService: background thread started (initial refresh will happen in background)")

    def list_mappers(self) -> list[dict]:
        return [self._sources[key].get_summary() for key in self._sources]

    def get_overlay(self, mapper_id: str) -> dict | None:
        source = self._sources.get(mapper_id)
        return source.get_overlay() if source else None

    def _loop(self):
        while True:
            started = time.monotonic()
            self._fortification.refresh_if_due()
            for source in self._sources.values():
                source.refresh_if_due()
            elapsed = time.monotonic() - started
            time.sleep(max(0, 30 - elapsed))
