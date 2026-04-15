(() => {
  // ── Map init ──────────────────────────────────────────────────────────────

  const map = L.map('map', { zoomControl: true }).setView([48.5, 31.2], 6);

  // ── Tilesets ──────────────────────────────────────────────────────────────

  const TILESETS = [
    {
      id: 'hybrid',
      label: 'Hybrid',
      base: {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        opts: { attribution: 'Tiles &copy; Esri', maxZoom: 18 },
      },
      overlays: [
        // Roads
        {
          url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}',
          opts: { attribution: '', maxZoom: 18 },
        },
        // City names + admin borders
        {
          url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
          opts: { attribution: '', maxZoom: 18 },
        },
      ],
    },
    {
      id: 'satellite',
      label: 'Satellite',
      base: {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        opts: { attribution: 'Tiles &copy; Esri', maxZoom: 18 },
      },
    },
    {
      id: 'dark',
      label: 'Dark',
      base: {
        url: 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
        opts: { attribution: '&copy; CartoDB', maxZoom: 19, subdomains: 'abcd' },
      },
    },
    {
      id: 'street',
      label: 'Street',
      base: {
        url: 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        opts: { attribution: '&copy; OpenStreetMap', maxZoom: 19 },
      },
    },
    {
      id: 'topo',
      label: 'Topo',
      base: {
        url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}',
        opts: { attribution: 'Tiles &copy; Esri', maxZoom: 18 },
      },
    },
  ];

  let _activeBaseId = 'hybrid';
  let _activeBaseTile = null;
  let _activeOverlayTiles = [];

  function applyTileset(id) {
    const tileset = TILESETS.find(t => t.id === id);
    if (!tileset) return;

    if (_activeBaseTile) map.removeLayer(_activeBaseTile);
    _activeOverlayTiles.forEach(t => map.removeLayer(t));
    _activeOverlayTiles = [];

    _activeBaseTile = L.tileLayer(tileset.base.url, tileset.base.opts).addTo(map);

    (tileset.overlays || []).forEach(ov => {
      _activeOverlayTiles.push(L.tileLayer(ov.url, ov.opts).addTo(map));
    });

    _activeBaseId = id;
  }

  function switchTileset(id) {
    if (id === _activeBaseId) return;
    applyTileset(id);
    document.querySelectorAll('.tile-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.tile === id);
    });
  }

  function buildTileSwitcher() {
    const container = document.getElementById('tile-switcher');
    container.innerHTML = TILESETS.map(t => `
      <button class="tile-btn ${t.id === _activeBaseId ? 'active' : ''}" data-tile="${t.id}">
        ${t.label}
      </button>
    `).join('');
    container.querySelectorAll('.tile-btn').forEach(btn => {
      btn.addEventListener('click', () => switchTileset(btn.dataset.tile));
    });
  }

  applyTileset('hybrid');
  buildTileSwitcher();

  // ── Layer groups ──────────────────────────────────────────────────────────

  const iswGroup = L.layerGroup().addTo(map);
  const cityGroup = L.layerGroup().addTo(map);

  const LAYER_STYLES = {
    infiltration: { color: '#b59f3b', fillColor: '#d4b96a', weight: 1.5, fillOpacity: 0.35 },
    gains:        { color: '#cc3333', fillColor: '#e05555', weight: 1.5, fillOpacity: 0.35 },
    advances:     { color: '#cc6622', fillColor: '#e08844', weight: 1.5, fillOpacity: 0.35 },
  };

  // Capture → red, Capture All → orange, Enter → yellow
  const CITY_COLORS = {
    capture_all: '#cc3333',
    capture:     '#cc6622',
    enter:       '#b59f3b',
  };

  // ── ISW layers ────────────────────────────────────────────────────────────

  function renderISWLayers(data) {
    iswGroup.clearLayers();
    if (!data || !data.layers) return;
    // Render in order: advances first (bottom), then gains, then infiltration (top)
    for (const name of ['advances', 'gains', 'infiltration']) {
      const fc = data.layers[name];
      if (!fc) continue;
      L.geoJSON(fc, { style: LAYER_STYLES[name] }).addTo(iswGroup);
    }
  }

  function updateFreshness(lastUpdated) {
    const el = document.getElementById('isw-freshness');
    if (!lastUpdated) { el.textContent = 'ISW: unavailable'; return; }
    const ageS = Math.round(Date.now() / 1000 - lastUpdated);
    const ageStr = ageS < 60 ? `${ageS}s ago` : `${Math.round(ageS / 60)}m ago`;
    el.textContent = `ISW: updated ${ageStr}`;
  }

  async function refreshISWLayers() {
    const data = await API.fetchISWLayers();
    if (data) {
      renderISWLayers(data);
      updateFreshness(data.last_updated);
    }
  }

  // ── City markers ──────────────────────────────────────────────────────────

  function cityMarketPriority(markets) {
    if ((markets.capture_all || []).length > 0) return 'capture_all';
    if ((markets.capture || []).length > 0) return 'capture';
    if ((markets.enter || []).length > 0) return 'enter';
    return null;
  }

  function computeCentroid(geojsonPolygon) {
    if (!geojsonPolygon || !geojsonPolygon.coordinates || !geojsonPolygon.coordinates[0]) return null;
    const ring = geojsonPolygon.coordinates[0];
    if (!ring.length) return null;
    let sumLon = 0, sumLat = 0;
    for (const [lon, lat] of ring) { sumLon += lon; sumLat += lat; }
    return [sumLat / ring.length, sumLon / ring.length]; // Leaflet uses [lat, lon]
  }

  function renderCityMarkers(cityMap) {
    cityGroup.clearLayers();
    const cities = cityMap.cities || {};
    for (const [cityId, cityEntry] of Object.entries(cities)) {
      const markets = cityEntry.markets || {};
      const priority = cityMarketPriority(markets);
      if (!priority) continue;

      const centroid = computeCentroid(cityEntry.geometry);
      if (!centroid) continue;

      const color = CITY_COLORS[priority];
      const marker = L.circleMarker(centroid, {
        radius: 7,
        color,
        weight: 2,
        fillColor: color,
        fillOpacity: 0.25,
      });

      const cityName = (cityEntry.city || {}).name_en || 'Unknown';
      marker.bindTooltip(cityName, { permanent: false, direction: 'top', offset: [0, -8] });
      marker.on('click', () => window.Panel.open(cityId, cityEntry));
      marker.addTo(cityGroup);
    }
  }

  // ── Startup ───────────────────────────────────────────────────────────────

  async function init() {
    const [layerData, cityMap] = await Promise.all([
      API.fetchISWLayers(),
      API.fetchCityMarketMap(),
    ]);

    if (layerData) {
      renderISWLayers(layerData);
      updateFreshness(layerData.last_updated);
    }

    if (cityMap) {
      renderCityMarkers(cityMap);
      window._cityMap = cityMap;
    }
  }

  init();

  // Auto-refresh ISW layers every 30s, update freshness label every 10s
  setInterval(refreshISWLayers, 30_000);
  setInterval(() => {
    const el = document.getElementById('isw-freshness');
    const match = el.textContent.match(/updated (\d+)s ago/);
    if (match) {
      const newAge = parseInt(match[1]) + 10;
      const ageStr = newAge < 60 ? `${newAge}s ago` : `${Math.round(newAge / 60)}m ago`;
      el.textContent = `ISW: updated ${ageStr}`;
    }
  }, 10_000);
})();
