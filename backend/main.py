import logging
import hashlib
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

# Make local `uvicorn main:app` and `uvicorn backend.main:app` launches behave
# consistently. Explicit shell/systemd environment variables retain priority.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

try:
    from . import city_map
    from .mapper_service import MapperService
    from .geolocation_service import GeolocationsService
    from .market_map_update_service import MarketMapUpdateService
    from .polymarket_service import PolymarketDataService
    from .database import PostGISDatabase, postgis_enabled
    from .temporal_repository import TemporalDataError, TemporalMapRepository
except ImportError:  # current deployment starts uvicorn from backend/
    import city_map
    from mapper_service import MapperService
    from geolocation_service import GeolocationsService
    from market_map_update_service import MarketMapUpdateService
    from polymarket_service import PolymarketDataService
    from database import PostGISDatabase, postgis_enabled
    from temporal_repository import TemporalDataError, TemporalMapRepository

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mapper_service = MapperService()
POSTGIS_CONFIGURED = postgis_enabled()
POSTGIS_READS_ENABLED = POSTGIS_CONFIGURED and os.getenv("WARDOTFUN_POSTGIS_READS_ENABLED", "0") == "1"
VECTOR_TILES_ENABLED = POSTGIS_CONFIGURED and os.getenv("WARDOTFUN_VECTOR_TILES_ENABLED", "0") == "1"
MAP_CHANGES_ENABLED = VECTOR_TILES_ENABLED and os.getenv("WARDOTFUN_MAP_CHANGES_ENABLED", "0") == "1"
temporal_repository = TemporalMapRepository() if POSTGIS_CONFIGURED else None
geo_service = GeolocationsService(
    read_only=POSTGIS_READS_ENABLED,
    use_postgis=POSTGIS_READS_ENABLED,
)
polymarket_service = PolymarketDataService()
_json_cache: dict[str, tuple[str, bytes]] = {}
_json_cache_bytes = 0
_json_cache_lock = threading.Lock()
_JSON_CACHE_MAX_BYTES = 96 * 1024 * 1024


def _conditional_json(
    request: Request,
    payload,
    *,
    cache_control: str = "no-cache",
    cache_key: str | None = None,
) -> Response:
    global _json_cache_bytes
    encoded = None
    etag = None
    if cache_key:
        with _json_cache_lock:
            cached = _json_cache.get(cache_key)
        if cached:
            etag, encoded = cached
    if encoded is None:
        encoded = json.dumps(
            jsonable_encoder(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
        if cache_key and len(encoded) <= _JSON_CACHE_MAX_BYTES:
            with _json_cache_lock:
                previous = _json_cache.pop(cache_key, None)
                if previous:
                    _json_cache_bytes -= len(previous[1])
                while _json_cache and _json_cache_bytes + len(encoded) > _JSON_CACHE_MAX_BYTES:
                    oldest_key = next(iter(_json_cache))
                    _json_cache_bytes -= len(_json_cache.pop(oldest_key)[1])
                _json_cache[cache_key] = (etag, encoded)
                _json_cache_bytes += len(encoded)
    headers = {
        "ETag": etag,
        "Cache-Control": cache_control,
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("if-none-match") in {etag, "*"}:
        return Response(status_code=304, headers=headers)
    return Response(content=encoded, media_type="application/json", headers=headers)


def _legacy_map_state(selected: str | None = None) -> dict:
    try:
        geolocations = geo_service.get_all(date=selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    chosen = geolocations["date"]
    mappers = []
    for mapper in mapper_service.list_mappers():
        mappers.append({
            **mapper,
            "mapper_id": mapper["id"],
            "kind": "mapper",
            "snapshot_id": None,
            "snapshot_date": None,
            "available": False,
            "layers": [],
            "tile_url": None,
        })
    return {
        "date": chosen,
        "timezone": "Europe/Kyiv",
        "available_dates": geolocations["dates"],
        "vector_tiles_enabled": False,
        "mappers": mappers,
        "fortifications": None,
        "geoconfirmed": {
            "date": chosen,
            "event_count": len(geolocations["events"]),
            "available": True,
            "last_updated": geolocations["sources"][0].get("last_updated"),
        },
    }


def reload_market_archive():
    data = city_map.reload()
    polymarket_service.invalidate()
    return data


market_map_update_service = MarketMapUpdateService(on_updated=reload_market_archive)


@asynccontextmanager
async def lifespan(app: FastAPI):
    city_map.load()
    if VECTOR_TILES_ENABLED:
        if os.getenv("WARDOTFUN_AUTO_MIGRATE", "0") == "1":
            PostGISDatabase().migrate()
        # Legacy routes remain live during the one-week compatibility window,
        # but uvicorn never performs upstream writes in PostGIS mode.
        mapper_service.load_caches()
    else:
        mapper_service.start()
    geo_service.start()
    polymarket_service.start()
    market_map_update_service.start()
    try:
        yield
    finally:
        geo_service.stop()
        mapper_service.stop()
        market_map_update_service.stop()


app = FastAPI(title="wardotfun API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)


@app.get("/health")
def health():
    storage = "postgis" if POSTGIS_READS_ENABLED else "shadow" if POSTGIS_CONFIGURED else "legacy"
    result = {
        "status": "ok",
        "storage": storage,
        "vector_tiles_enabled": VECTOR_TILES_ENABLED,
        "map_changes_enabled": MAP_CHANGES_ENABLED,
    }
    if temporal_repository:
        try:
            temporal = temporal_repository.health()
            result["temporal"] = temporal
            if temporal["status"] != "ok":
                result["status"] = "degraded"
        except Exception as exc:
            logger.exception("Temporal health query failed")
            result.update({"status": "degraded", "temporal": {"status": "error", "error": str(exc)}})
    return result


@app.get("/api/mappers")
def mappers():
    return {"mappers": mapper_service.list_mappers()}


@app.get("/api/mapper-overlay")
def mapper_overlay(request: Request, mapper: str):
    overlay = mapper_service.get_overlay(mapper)
    if not overlay:
        raise HTTPException(status_code=404, detail=f"unknown mapper: {mapper}")
    version = f"{overlay.get('last_updated') or 0}:{overlay.get('status')}"
    return _conditional_json(
        request,
        overlay,
        cache_key=f"legacy-mapper:{mapper}:{version}",
    )



@app.get("/api/fortifications")
def fortifications(request: Request):
    overlay = mapper_service.get_fortification_overlay()
    version = f"{overlay.get('last_updated') or 0}:{overlay.get('status')}"
    return _conditional_json(
        request,
        overlay,
        cache_key=f"legacy-fortifications:{version}",
    )


@app.get("/api/map-state")
def map_state(request: Request, date: str | None = None):
    if not VECTOR_TILES_ENABLED or not temporal_repository:
        state = _legacy_map_state(date)
    else:
        try:
            state = temporal_repository.get_map_state(date)
        except TemporalDataError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Map-state query failed")
            raise HTTPException(status_code=503, detail="temporal map storage is unavailable") from exc
    state["map_changes_enabled"] = MAP_CHANGES_ENABLED
    # The manifest is intentionally tiny. Re-encode it so freshness/error changes
    # receive a new ETag even when all immutable snapshot IDs stay the same.
    return _conditional_json(request, state)


@app.get("/api/map-tiles/{source}/{snapshot}/{z}/{x}/{y}.pbf")
def map_tile(
    request: Request,
    source: str,
    snapshot: str,
    z: int,
    x: int,
    y: int,
):
    if not temporal_repository:
        raise HTTPException(status_code=404, detail="vector tiles are not enabled")
    try:
        tile, etag = temporal_repository.get_tile(source, snapshot, z, x, y)
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="map snapshot not found") from exc
    except Exception as exc:
        logger.exception("Vector tile query failed")
        raise HTTPException(status_code=503, detail="vector tile storage is unavailable") from exc
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=31536000, immutable",
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("if-none-match") in {etag, "*"}:
        return Response(status_code=304, headers=headers)
    return Response(
        content=tile,
        media_type="application/vnd.mapbox-vector-tile",
        headers=headers,
    )


def _require_map_changes():
    if not MAP_CHANGES_ENABLED or not temporal_repository:
        raise HTTPException(status_code=404, detail="map changes are not enabled")


@app.get("/api/map-changes")
def map_changes(
    request: Request,
    date: str | None = None,
    source: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
):
    _require_map_changes()
    try:
        payload = temporal_repository.get_map_changes(
            selected=date, source_id=source, cursor=cursor, limit=limit
        )
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Map-change feed query failed")
        raise HTTPException(status_code=503, detail="map-change storage is unavailable") from exc
    return _conditional_json(request, payload)


@app.get("/api/map-changes/status")
def map_change_status(request: Request, date: str | None = None, after: str | None = None):
    _require_map_changes()
    try:
        payload = temporal_repository.get_map_change_status(selected=date, after=after)
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Map-change status query failed")
        raise HTTPException(status_code=503, detail="map-change storage is unavailable") from exc
    return _conditional_json(request, payload)


@app.get("/api/map-changes/{area_id}")
def map_change(request: Request, area_id: str):
    _require_map_changes()
    try:
        payload = temporal_repository.get_map_change(area_id)
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="map change not found") from exc
    except Exception as exc:
        logger.exception("Map-change detail query failed")
        raise HTTPException(status_code=503, detail="map-change storage is unavailable") from exc
    return _conditional_json(request, payload, cache_control="public, max-age=31536000, immutable")


@app.get("/api/map-change-images/v3/{area_id}.svg")
def map_change_image(request: Request, area_id: str):
    _require_map_changes()
    try:
        svg, etag = temporal_repository.get_change_svg(area_id)
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="map change not found") from exc
    except Exception as exc:
        logger.exception("Map-change SVG render failed")
        raise HTTPException(status_code=503, detail="map-change image is unavailable") from exc
    headers = {
        "ETag": etag,
        "Cache-Control": "public, max-age=31536000, immutable",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") in {etag, "*"}:
        return Response(status_code=304, headers=headers)
    return Response(content=svg, media_type="image/svg+xml", headers=headers)


@app.get("/api/map-change-tiles/v3/{area_id}/{z}/{x}/{y}.pbf")
def map_change_tile(request: Request, area_id: str, z: int, x: int, y: int):
    _require_map_changes()
    try:
        tile, etag = temporal_repository.get_change_tile(area_id, z, x, y)
    except TemporalDataError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="map change not found") from exc
    except Exception as exc:
        logger.exception("Map-change tile query failed")
        raise HTTPException(status_code=503, detail="map-change tile is unavailable") from exc
    headers = {
        "ETag": etag, "Cache-Control": "public, max-age=31536000, immutable",
        "Vary": "Accept-Encoding",
    }
    if request.headers.get("if-none-match") in {etag, "*"}:
        return Response(status_code=304, headers=headers)
    return Response(content=tile, media_type="application/vnd.mapbox-vector-tile", headers=headers)


@app.get("/api/geolocations")
def geolocations(request: Request, date: str | None = None, q: str | None = None, faction: str | None = None,
                 icon: str | None = None, origin: str | None = None):
    try:
        payload = geo_service.get_all(date=date, q=q, faction=faction, icon=icon, origin=origin)
        source_version = payload.get("sources", [{}])[0].get("last_updated") or 0
        filter_key = hashlib.sha256(
            json.dumps([date, q, faction, icon, origin], separators=(",", ":")).encode()
        ).hexdigest()[:12]
        return _conditional_json(
            request,
            payload,
            cache_key=f"geolocations:{payload.get('date')}:{source_version}:{filter_key}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/geolocations/{uuid}")
def geolocation_detail(uuid: str):
    event = geo_service.get_detail(uuid)
    if not event:
        raise HTTPException(status_code=404, detail="geolocation not found")
    return event


@app.get("/api/geolocations/icons/{icon_id}")
def geolocation_icon(icon_id: str):
    cached = geo_service.get_icon(icon_id)
    if not cached:
        raise HTTPException(status_code=404, detail="geolocation icon not found")
    path, content_type = cached
    return Response(content=path.read_bytes(), media_type=content_type,
                    headers={"Cache-Control": "public, max-age=604800, immutable"})


@app.get("/api/city-market-map")
def get_city_market_map():
    return city_map.get()


@app.get("/api/market-data")
def get_market_data():
    return polymarket_service.get_data()


@app.post("/api/admin/reload-city-map")
def reload_city_map():
    data = reload_market_archive()
    return {"status": "ok", "cities": len(data.get("cities", {}))}


@app.get("/api/admin/market-map-updater")
def market_map_updater_status():
    return market_map_update_service.status()


# Mount frontend static files — must come AFTER all API routes
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s", FRONTEND_DIR)
