import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import city_map
from isw_poller import ISWPoller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

poller = ISWPoller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    city_map.load()
    poller.start()
    yield


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


@app.get("/api/isw-layers")
def isw_layers():
    layers = poller.get_layers()
    last_updated = poller.last_updated
    return {
        "layers": layers,
        "last_updated": last_updated,
    }


@app.get("/api/city-market-map")
def get_city_market_map():
    return city_map.get()


@app.post("/api/admin/reload-city-map")
def reload_city_map():
    data = city_map.reload()
    return {"status": "ok", "cities": len(data.get("cities", {}))}


# Mount frontend static files — must come AFTER all API routes
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning("Frontend directory not found at %s", FRONTEND_DIR)
