(async () => {
  // ── Tile styles ───────────────────────────────────────────────────────────

  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  const OFM_STYLE_URL = 'https://tiles.openfreemap.org/styles/liberty';

  let _ofmStyle = null;

  async function fetchOFMStyle() {
    if (_ofmStyle) return _ofmStyle;
    const resp = await fetch(OFM_STYLE_URL);
    _ofmStyle = await resp.json();
    return _ofmStyle;
  }

  // Scale the stop values in a MapLibre interpolate width expression by factor.
  function scaleWidth(expr, factor) {
    if (typeof expr === 'number') return expr * factor;
    if (!Array.isArray(expr) || expr[0] !== 'interpolate') return expr;
    const r = [...expr];
    for (let i = 4; i < r.length; i += 2) r[i] = r[i] * factor;
    return r;
  }

  async function buildHybridStyle() {
    const base = await fetchOFMStyle();
    const style = JSON.parse(JSON.stringify(base));
    style.layers = style.layers
      .filter(l => l.type === 'line' || l.type === 'symbol')
      .map(l => {
        if (l.type === 'symbol') {
          return { ...l, paint: { ...l.paint, 'text-color': '#ffffff', 'text-halo-color': '#000000', 'text-halo-width': 1.5 } };
        }
        if (l.id === 'boundary_2') {
          return { ...l, paint: { ...l.paint, 'line-color': '#aaaaaa', 'line-width': scaleWidth(l.paint['line-width'], 1.5), 'line-opacity': 1 } };
        }
        if (l.id === 'boundary_disputed') {
          return { ...l, paint: { ...l.paint, 'line-color': '#aaaaaa', 'line-width': scaleWidth(l.paint['line-width'], 1.5), 'line-opacity': 0.7 } };
        }
        if (l['source-layer'] === 'transportation') {
          return { ...l, paint: { ...l.paint, 'line-width': scaleWidth(l.paint['line-width'], 0.5) } };
        }
        if (l['source-layer'] === 'waterway' && (l.id === 'waterway_river' || l.id === 'waterway_other')) {
          return { ...l, paint: { ...l.paint, 'line-width': scaleWidth(l.paint['line-width'], 2.5) } };
        }
        return l;
      });
    style.sources['satellite'] = {
      type: 'raster',
      tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`],
      tileSize: 256, attribution: 'Tiles © Esri',
    };
    style.layers.unshift({ id: 'satellite-base', type: 'raster', source: 'satellite' });
    return style;
  }

  function makeRasterStyle(id, tiles, attribution) {
    return { version: 8, sources: { [id]: { type: 'raster', tiles, tileSize: 256, attribution } }, layers: [{ id, type: 'raster', source: id }] };
  }

  const TILESETS = [
    { id: 'hybrid',    label: 'Hybrid',    getStyle: buildHybridStyle },
    { id: 'satellite', label: 'Satellite', getStyle: () => makeRasterStyle('sat',  [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri') },
    { id: 'dark',      label: 'Dark',      getStyle: () => makeRasterStyle('dark', ['https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'], '© CartoDB') },
    { id: 'street',    label: 'Street',    getStyle: () => makeRasterStyle('osm',  ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], '© OpenStreetMap') },
    { id: 'topo',      label: 'Topo',      getStyle: () => makeRasterStyle('topo', [`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri') },
  ];

  // ── Layer/city styles ─────────────────────────────────────────────────────

  const ISW_STYLE = {
    control:      { color: '#8b1a1a', fill: 'rgba(139,26,26,0.3)'    },
    infiltration: { color: '#b59f3b', fill: 'rgba(212,185,106,0.35)' },
    gains:        { color: '#cc3333', fill: 'rgba(224,85,85,0.35)'   },
    advances:     { color: '#cc6622', fill: 'rgba(224,136,68,0.35)'  },
  };

  const CITY_COLORS = { capture_all: '#cc3333', capture: '#cc6622', enter: '#b59f3b' };

  // ── State ─────────────────────────────────────────────────────────────────

  let _activeId      = 'hybrid';
  let _iswData       = null;
  let _cityMap       = null;
  let _cityEntries   = {};
  let _targetEntries = {};
  let _lastUpdated   = null;

  // ── Init map (async — fetch style first, no blank placeholder) ────────────

  let initialStyle;
  try {
    initialStyle = await buildHybridStyle();
  } catch (e) {
    console.warn('OFM style fetch failed, falling back to satellite', e);
    initialStyle = makeRasterStyle('sat', [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri');
    _activeId = 'satellite';
  }

  const map = new maplibregl.Map({
    container: 'map',
    style: initialStyle,
    center: [31.2, 48.5],
    zoom: 6,
  });

  // ── Tile switcher ─────────────────────────────────────────────────────────

  function buildTileSwitcher() {
    const el = document.getElementById('tile-switcher');
    el.innerHTML = TILESETS.map(t =>
      `<button class="tile-btn ${t.id === _activeId ? 'active' : ''}" data-tile="${t.id}">${t.label}</button>`
    ).join('');
    el.querySelectorAll('.tile-btn').forEach(btn =>
      btn.addEventListener('click', () => switchTileset(btn.dataset.tile))
    );
  }

  async function switchTileset(id) {
    if (id === _activeId) return;
    const tileset = TILESETS.find(t => t.id === id);
    if (!tileset) return;
    _activeId = id;
    document.querySelectorAll('.tile-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.tile === id)
    );
    const style = await tileset.getStyle();
    // transformStyle merges our custom sources/layers into the new basemap
    // instead of wiping them — no re-add needed after the switch.
    map.setStyle(style, {
      transformStyle: (prevStyle, nextStyle) => {
        if (!prevStyle) return nextStyle;
        const customSources = {};
        for (const [sid, src] of Object.entries(prevStyle.sources || {})) {
          if (sid.startsWith('isw-') || sid === 'cities' || sid === 'targets') customSources[sid] = src;
        }
        const customLayers = (prevStyle.layers || []).filter(l =>
          l.id.startsWith('isw-') || l.id.startsWith('cities-') || l.id.startsWith('targets-')
        );
        return {
          ...nextStyle,
          sources: { ...nextStyle.sources, ...customSources },
          layers: [...nextStyle.layers, ...customLayers],
        };
      },
    });
  }

  buildTileSwitcher();

  // ── Data layers ───────────────────────────────────────────────────────────

  function readdDataLayers() {
    if (_iswData) addISWLayers(_iswData);
    if (_cityMap) { addCityLayers(_cityMap); addTargetLayers(_cityMap); }
  }

  function addISWLayers(data) {
    if (!data?.layers) return;
    for (const name of ['control', 'advances', 'gains', 'infiltration']) {
      const fc = data.layers[name];
      if (!fc) continue;
      const sid = `isw-${name}`;
      if (map.getSource(sid)) {
        map.getSource(sid).setData(fc);
      } else {
        map.addSource(sid, { type: 'geojson', data: fc });
        map.addLayer({ id: `${sid}-fill`, type: 'fill', source: sid, paint: { 'fill-color': ISW_STYLE[name].fill } });
        map.addLayer({ id: `${sid}-line`, type: 'line', source: sid, paint: { 'line-color': ISW_STYLE[name].color, 'line-width': 1.5 } });
      }
    }
  }

  // ── City layers ───────────────────────────────────────────────────────────

  function priority(markets) {
    if ((markets.capture_all || []).length) return 'capture_all';
    if ((markets.capture     || []).length) return 'capture';
    if ((markets.enter       || []).length) return 'enter';
    return null;
  }

  function addCityLayers(cityMap) {
    _cityEntries = {};
    const features = [];
    for (const [cityId, entry] of Object.entries(cityMap.cities || {})) {
      const p = priority(entry.markets || {});
      if (!p || !entry.geometry) continue;
      _cityEntries[cityId] = entry;
      features.push({
        type: 'Feature',
        id: cityId,
        geometry: entry.geometry,
        properties: { cityId, name: (entry.city || {}).name_en || 'Unknown', color: CITY_COLORS[p] },
      });
    }

    const fc = { type: 'FeatureCollection', features };

    if (map.getSource('cities')) {
      map.getSource('cities').setData(fc);
      return;
    }

    map.addSource('cities', { type: 'geojson', data: fc });
    // Transparent fill — hit detection only (hover/click events fire on fill, not line)
    map.addLayer({ id: 'cities-fill',       type: 'fill', source: 'cities', paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0 } });
    // White casing underneath for contrast against dark ISW fills
    map.addLayer({ id: 'cities-line-case',  type: 'line', source: 'cities', paint: { 'line-color': '#ffffff', 'line-width': 5, 'line-opacity': 0.5 } });
    // Colored dashed border — visually distinct from solid ISW territory lines
    map.addLayer({ id: 'cities-line',       type: 'line', source: 'cities', paint: { 'line-color': ['get', 'color'], 'line-width': 3, 'line-dasharray': [4, 1.5], 'line-opacity': 1 } });
    // Subtle fill on hover — just enough to show which polygon is active
    map.addLayer({ id: 'cities-fill-hover', type: 'fill', source: 'cities', paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.15 }, filter: ['==', ['get', 'cityId'], ''] });
  }

  // ── Target layers (capture market specific points) ────────────────────────

  function addTargetLayers(cityMap) {
    _targetEntries = {};
    const features = [];

    for (const [cityId, entry] of Object.entries(cityMap.cities || {})) {
      const captureMarkets = (entry.markets?.capture || []).filter(m => m.target && m.active !== false);
      if (!captureMarkets.length) continue;

      // Group markets by exact target coords (multiple deadlines can share one target)
      const byCoord = {};
      for (const m of captureMarkets) {
        const key = `${m.target.lon},${m.target.lat}`;
        if (!byCoord[key]) byCoord[key] = [];
        byCoord[key].push(m);
      }

      for (const [coordKey, markets] of Object.entries(byCoord)) {
        const [lon, lat] = coordKey.split(',').map(Number);
        const targetKey = `${cityId}::${coordKey}`;
        _targetEntries[targetKey] = {
          cityEntry: { ...entry, markets: { enter: [], capture: markets, capture_all: [] } },
          lngLat: [lon, lat],
        };
        features.push({
          type: 'Feature',
          properties: { targetKey, cityName: (entry.city || {}).name_en || 'Unknown' },
          geometry: { type: 'Point', coordinates: [lon, lat] },
        });
      }
    }

    const fc = { type: 'FeatureCollection', features };

    if (map.getSource('targets')) {
      map.getSource('targets').setData(fc);
      return;
    }

    map.addSource('targets', { type: 'geojson', data: fc });
    map.addLayer({
      id: 'targets-circle',
      type: 'circle',
      source: 'targets',
      paint: {
        'circle-radius': 5,
        'circle-color': '#cc6622',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
        'circle-opacity': 0.95,
      },
    });
    map.addLayer({
      id: 'targets-circle-hover',
      type: 'circle',
      source: 'targets',
      paint: {
        'circle-radius': 8,
        'circle-color': '#cc6622',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
        'circle-opacity': 1,
      },
      filter: ['==', ['get', 'targetKey'], ''],
    });
  }

  // City events registered ONCE on the map object — survive setStyle.
  const _cityPopup = new maplibregl.Popup({ closeButton: false, offset: 8 });

  map.on('mousemove', 'cities-fill', e => {
    if (!e.features.length) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    map.getCanvas().style.cursor = 'pointer';
    const f = e.features[0];
    map.setFilter('cities-fill-hover', ['==', ['get', 'cityId'], f.properties.cityId]);
    _cityPopup.setLngLat(e.lngLat).setText(f.properties.name).addTo(map);
  });

  map.on('mouseleave', 'cities-fill', () => {
    map.getCanvas().style.cursor = '';
    map.setFilter('cities-fill-hover', ['==', ['get', 'cityId'], '']);
    _cityPopup.remove();
  });

  map.on('click', 'cities-fill', e => {
    if (!e.features.length) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    const { cityId } = e.features[0].properties;
    const entry = _cityEntries[cityId];
    if (entry) window.Panel.open(cityId, entry, e.lngLat, e.point, map);
  });

  map.on('mousemove', 'targets-circle', e => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = 'pointer';
    const f = e.features[0];
    map.setFilter('targets-circle-hover', ['==', ['get', 'targetKey'], f.properties.targetKey]);
    map.setFilter('cities-fill-hover', ['==', ['get', 'cityId'], '']);
    _cityPopup.setLngLat(e.lngLat).setText(`${f.properties.cityName} — capture target`).addTo(map);
  });

  map.on('mouseleave', 'targets-circle', () => {
    map.getCanvas().style.cursor = '';
    map.setFilter('targets-circle-hover', ['==', ['get', 'targetKey'], '']);
    _cityPopup.remove();
  });

  map.on('click', 'targets-circle', e => {
    if (!e.features.length) return;
    const { targetKey } = e.features[0].properties;
    const t = _targetEntries[targetKey];
    if (t) window.Panel.open(targetKey, t.cityEntry, t.lngLat, e.point, map);
  });

  // ── Freshness ─────────────────────────────────────────────────────────────

  function renderFreshness() {
    const el = document.getElementById('isw-freshness');
    if (!_lastUpdated) { el.textContent = 'ISW: unavailable'; return; }
    const s = Math.round(Date.now() / 1000 - _lastUpdated);
    el.textContent = `ISW: updated ${s < 60 ? `${s}s` : `${Math.round(s / 60)}m`} ago`;
  }

  // ── Initial data load — fires once when the map first loads ───────────────

  map.once('load', async () => {
    const [layerData, cityMap] = await Promise.all([
      API.fetchISWLayers(),
      API.fetchCityMarketMap(),
    ]);
    if (layerData) { _iswData = layerData; addISWLayers(layerData); _lastUpdated = layerData.last_updated; renderFreshness(); }
    if (cityMap)   { _cityMap = cityMap;   addCityLayers(cityMap); addTargetLayers(cityMap); }
  });

  setInterval(async () => {
    const data = await API.fetchISWLayers();
    if (data && map.isStyleLoaded()) { _iswData = data; addISWLayers(data); _lastUpdated = data.last_updated; renderFreshness(); }
  }, 30_000);

  setInterval(renderFreshness, 10_000);
})();
