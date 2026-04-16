import logging
import pickle
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen
import json

from geo_utils import arcgis_features_to_geojson

CACHE_DIR = Path(__file__).parent / "data" / "layer_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)

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

POLL_INTERVAL = 30  # seconds

# Per-layer TTL overrides — layers not listed here use POLL_INTERVAL
LAYER_TTL = {
    "control": 24 * 60 * 60,  # 24h — Russian control changes slowly
}

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
        self._last_fetched: dict[str, float] = {}  # per-layer fetch timestamps
        self._last_updated: float | None = None
        self._thread: threading.Thread | None = None

    def get_layers(self) -> dict:
        with self._lock:
            return dict(self._cache)

    @property
    def last_updated(self) -> float | None:
        return self._last_updated

    def _should_fetch(self, name: str, force: bool = False) -> bool:
        if force:
            return True
        ttl = LAYER_TTL.get(name, POLL_INTERVAL)
        last = self._last_fetched.get(name, 0)
        return (time.time() - last) >= ttl

    def _save(self, name: str, fc: dict, fetched_at: float):
        path = CACHE_DIR / f"{name}.pkl"
        try:
            path.write_bytes(pickle.dumps({"fc": fc, "fetched_at": fetched_at}))
        except Exception as exc:
            logger.warning("Failed to save cache for %s: %s", name, exc)

    def _load(self, name: str) -> float | None:
        """Load cached layer from disk. Returns fetched_at timestamp if loaded, else None."""
        path = CACHE_DIR / f"{name}.pkl"
        if not path.exists():
            return None
        try:
            data = pickle.loads(path.read_bytes())
            fc, fetched_at = data["fc"], data["fetched_at"]
            with self._lock:
                self._cache[name] = fc
            self._last_fetched[name] = fetched_at
            logger.info("ISW layer %s: loaded from cache (%d features, age %.0fs)",
                        name, len(fc["features"]), time.time() - fetched_at)
            return fetched_at
        except Exception as exc:
            logger.warning("Failed to load cache for %s: %s", name, exc)
            return None

    def _fetch_layer_cached(self, name: str, url: str, force: bool = False):
        if not self._should_fetch(name, force):
            return
        try:
            fc = _fetch_layer(name, url)
            fetched_at = time.time()
            with self._lock:
                self._cache[name] = fc
            self._last_fetched[name] = fetched_at
            self._save(name, fc, fetched_at)
            logger.info("ISW layer %s: %d features", name, len(fc["features"]))
        except Exception as exc:
            logger.warning("Failed to fetch ISW layer %s: %s", name, exc)

    def _fetch_all(self, force: bool = False):
        for name, url in LAYERS.items():
            self._fetch_layer_cached(name, url, force=force)
        self._last_updated = time.time()

    def _loop(self):
        while True:
            start = time.monotonic()
            self._fetch_all()
            elapsed = time.monotonic() - start
            time.sleep(max(0, POLL_INTERVAL - elapsed))

    def start(self):
        """Load cached layers from disk, fetch any that are stale, then start poll loop."""
        logger.info("ISWPoller: loading cached layers...")
        for name in LAYERS:
            self._load(name)

        logger.info("ISWPoller: fetching stale layers...")
        self._fetch_all(force=False)

        self._last_updated = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("ISWPoller: background thread started (interval=%ds)", POLL_INTERVAL)
