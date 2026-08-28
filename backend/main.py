import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import city_map
from mapper_service import MapperService
from geolocation_service import GeolocationsService
from market_map_update_service import MarketMapUpdateService
from polymarket_service import PolymarketDataService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

mapper_service = MapperService()
geo_service = GeolocationsService()
polymarket_service = PolymarketDataService()


def reload_market_archive():
    data = city_map.reload()
    polymarket_service.invalidate()
    return data


market_map_update_service = MarketMapUpdateService(on_updated=reload_market_archive)


@asynccontextmanager
async def lifespan(app: FastAPI):
    city_map.load()
    mapper_service.start()
    geo_service.start()
    polymarket_service.start()
    market_map_update_service.start()
    try:
        yield
    finally:
        geo_service.stop()
        market_map_update_service.stop()


app = FastAPI(title="wardotfun API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/mappers")
def mappers():
    return {"mappers": mapper_service.list_mappers()}


@app.get("/api/mapper-overlay")
def mapper_overlay(mapper: str):
    overlay = mapper_service.get_overlay(mapper)
    if not overlay:
        raise HTTPException(status_code=404, detail=f"unknown mapper: {mapper}")
    return overlay



@app.get("/api/fortifications")
def fortifications():
    return mapper_service.get_fortification_overlay()


@app.get("/api/geolocations")
def geolocations(date: str | None = None, q: str | None = None, faction: str | None = None,
                 icon: str | None = None, origin: str | None = None):
    try:
        return geo_service.get_all(date=date, q=q, faction=faction, icon=icon, origin=origin)
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
