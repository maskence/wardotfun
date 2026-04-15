import logging
import threading
import time
from urllib.request import Request, urlopen
import json

from geo_utils import arcgis_features_to_geojson

logger = logging.getLogger(__name__)

LAYERS = {
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

POLL_INTERVAL = 30  # seconds

EMPTY_FC = {"type": "FeatureCollection", "features": []}


def _fetch_layer(name: str, url: str) -> dict:
    query_url = (
        f"{url}/query?where=1%3D1&outFields=*&returnGeometry=true"
        "&cacheHint=true&f=json"
    )
    req = Request(query_url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    error = data.get("error") if isinstance(data, dict) else None
    if error:
        raise RuntimeError(f"ArcGIS error for {name}: {error}")
    features = data.get("features") or []
    return arcgis_features_to_geojson(features)


class ISWPoller:
    def __init__(self):
        self._cache: dict[str, dict] = {name: EMPTY_FC for name in LAYERS}
        self._lock = threading.Lock()
        self._last_updated: float | None = None
        self._thread: threading.Thread | None = None

    def get_layers(self) -> dict:
        with self._lock:
            return dict(self._cache)

    @property
    def last_updated(self) -> float | None:
        return self._last_updated

    def _fetch_all(self):
        for name, url in LAYERS.items():
            try:
                fc = _fetch_layer(name, url)
                with self._lock:
                    self._cache[name] = fc
                logger.info("ISW layer %s: %d features", name, len(fc["features"]))
            except Exception as exc:
                logger.warning("Failed to fetch ISW layer %s: %s", name, exc)
        self._last_updated = time.time()

    def _loop(self):
        while True:
            start = time.monotonic()
            self._fetch_all()
            elapsed = time.monotonic() - start
            time.sleep(max(0, POLL_INTERVAL - elapsed))

    def start(self):
        """Fetch all layers once synchronously, then start the background poll loop."""
        logger.info("ISWPoller: initial fetch...")
        self._fetch_all()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("ISWPoller: background thread started (interval=%ds)", POLL_INTERVAL)
