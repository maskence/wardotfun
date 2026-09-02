(async () => {
  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  const OFM_STYLE_URL = 'https://tiles.openfreemap.org/styles/liberty';

  let _ofmStyle = null;

  async function fetchOFMStyle() {
    if (_ofmStyle) return _ofmStyle;
    const resp = await fetch(OFM_STYLE_URL);
    _ofmStyle = await resp.json();
    return _ofmStyle;
  }

  function scaleWidth(expr, factor) {
    if (typeof expr === 'number') return expr * factor;
    if (!Array.isArray(expr) || expr[0] !== 'interpolate') return expr;
    const scaled = [...expr];
    for (let i = 4; i < scaled.length; i += 2) scaled[i] = scaled[i] * factor;
    return scaled;
  }

  function marketCityBasemapFilter(baseFilter) {
    if (!_marketCityNames.length) return baseFilter;
    const names = ['literal', _marketCityNames];
    const excludesMarketCities = ['all',
      ['!', ['in', ['coalesce', ['get', 'name_en'], ''], names]],
      ['!', ['in', ['coalesce', ['get', 'name:latin'], ''], names]],
    ];
    return baseFilter ? ['all', baseFilter, excludesMarketCities] : excludesMarketCities;
  }

  async function buildHybridStyle() {
    const base = await fetchOFMStyle();
    const style = JSON.parse(JSON.stringify(base));
    const settlementMinZoom = {
      label_town: 7,
      label_village: 10.5,
      label_other: 11.5,
    };
    style.layers = style.layers
      .filter(layer => layer.type === 'line' || layer.type === 'symbol')
      .map(layer => {
        if (layer.type === 'symbol') {
          const isPlaceLabel = layer['source-layer'] === 'place';
          const placeFilter = isPlaceLabel ? marketCityBasemapFilter(layer.filter) : layer.filter;
          return {
            ...layer,
            ...(settlementMinZoom[layer.id] !== undefined ? { minzoom: settlementMinZoom[layer.id] } : {}),
            ...(placeFilter ? { filter: placeFilter } : {}),
            metadata: {
              ...layer.metadata,
              ...(isPlaceLabel ? { 'wardotfun:base-filter': layer.filter || null } : {}),
            },
            layout: {
              ...layer.layout,
              ...(isPlaceLabel ? {
                'text-field': ['coalesce', ['get', 'name_en'], ['get', 'name:latin']],
              } : {}),
            },
            paint: {
              ...layer.paint,
              'text-color': '#ffffff',
              'text-halo-color': '#000000',
              'text-halo-width': 1.5,
            },
          };
        }
        if (layer.id === 'boundary_2') {
          return {
            ...layer,
            paint: {
              ...layer.paint,
              'line-color': '#aaaaaa',
              'line-width': scaleWidth(layer.paint['line-width'], 1.5),
              'line-opacity': 1,
            },
          };
        }
        if (layer.id === 'boundary_disputed') {
          return {
            ...layer,
            paint: {
              ...layer.paint,
              'line-color': '#aaaaaa',
              'line-width': scaleWidth(layer.paint['line-width'], 1.5),
              'line-opacity': 0.7,
            },
          };
        }
        if (layer['source-layer'] === 'transportation') {
          return {
            ...layer,
            paint: {
              ...layer.paint,
              'line-width': scaleWidth(layer.paint['line-width'], 0.5),
              'line-opacity': ['interpolate', ['linear'], ['zoom'], 7, 0, 10, 0.2, 14, 0.5],
            },
          };
        }
        if (layer['source-layer'] === 'waterway' && (layer.id === 'waterway_river' || layer.id === 'waterway_other')) {
          return {
            ...layer,
            paint: {
              ...layer.paint,
              'line-width': scaleWidth(layer.paint['line-width'], 2.5),
            },
          };
        }
        return layer;
      });

    style.sources.satellite = {
      type: 'raster',
      tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`],
      tileSize: 256,
      attribution: 'Tiles © Esri',
    };
    style.layers.unshift({ id: 'satellite-base', type: 'raster', source: 'satellite' });
    return style;
  }

  function makeRasterStyle(id, tiles, attribution) {
    return {
      version: 8,
      glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
      sources: { [id]: { type: 'raster', tiles, tileSize: 256, attribution } },
      layers: [{ id, type: 'raster', source: id }],
    };
  }

  // MapLibre creates tile Requests inside a worker. Unlike window.fetch(), the
  // Request constructor used there cannot resolve root-relative URL templates.
  // Resolve API templates on the main thread while preserving {z}/{x}/{y}.
  function absoluteTileUrl(template) {
    if (!template) return template;
    try {
      return new URL(template, window.location.href).href
        .replace(/%7B(z|x|y)%7D/gi, '{$1}');
    } catch (_error) {
      return template;
    }
  }

  const TILESETS = [
    { id: 'hybrid', label: 'Hybrid', group: 'Reference', getStyle: buildHybridStyle },
    { id: 'street', label: 'Street', group: 'Reference', getStyle: () => makeRasterStyle('osm', ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'], '© OpenStreetMap') },
    { id: 'dark', label: 'Dark', group: 'Reference', getStyle: () => makeRasterStyle('dark', ['https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png'], '© CartoDB') },
    { id: 'topo', label: 'Topographic', group: 'Terrain', getStyle: () => makeRasterStyle('topo', [`${ESRI}/World_Topo_Map/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri') },
    { id: 'satellite', label: 'Satellite', group: 'Imagery', getStyle: () => makeRasterStyle('sat', [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri') },
  ];

  const CITY_COLORS = { capture_all: '#cc3333', capture: '#cc6622', enter: '#b59f3b' };

  const MAP_ICONS = {
    'market-city-icon': '/icons/market-city.svg',
    'market-target-icon': '/icons/market-target.svg',
    'geolocation-icon': '/icons/geolocation.svg',
  };

  const CITY_LEGEND_ITEMS = [
    { label: 'Enter city',       color: '#ffffff', borderColor: '#b59f3b', swatch: 'symbol', icon: MAP_ICONS['market-city-icon'] },
    { label: 'Capture',          color: '#ffffff', borderColor: '#cc3333', swatch: 'symbol', icon: MAP_ICONS['market-city-icon'] },
    { label: 'Capture all',      color: '#ffffff', borderColor: '#cc3333', swatch: 'symbol', icon: MAP_ICONS['market-city-icon'] },
    { label: 'Capture target',   color: '#cc3333', swatch: 'symbol', icon: MAP_ICONS['market-target-icon'] },
  ];

  const GEO_COLORS = { ru: '#cc3333', ua: '#4488cc', unknown: '#888888' };

  let _activeTilesetId = 'hybrid';
  let _mappers = [];
  let _activeMapperId = null;
  let _overlayData = null;
  let _fortData = null;
  let _geoData = null;
  let _cityMap = null;
  let _cityEntries = {};
  let _targetEntries = {};
  let _overlaySourceIds = [];
  let _overlayLayerIds = [];
  let _fortSourceIds = [];
  let _fortLayerIds = [];
  let _basemapSourceIds = [];
  let _basemapLayerIds = [];
  const _fortLayerVisibility = new Map();
  let _marketCityNames = [];
  let _geoPopup = null;
  let _twitterWidgetsPromise = null;
  let _geoDates = [];
  let _geoDate = null;
  let _geoRequestSerial = 0;
  let _geoEventsBound = false;
  let _startupInitialized = false;
  let _mapState = null;
  let _temporalMode = false;
  let _changeComparison = null;
  let _changeSourceId = null;
  let _changeLayerIds = [];
  const _fortStyleLayersByDataId = new Map();

  // Start application data immediately. None of these requests should wait for
  // raster/vector tiles or other MapLibre source loading.
  const startupMapStatePromise = API.startup.mapState;
  const startupMappersPromise = API.startup.mappers;
  const startupGeoPromise = API.startup.geolocations;
  const startupCityPromise = API.startup.cityMap;
  const startupMarketPromise = API.startup.marketData;
  // A temporal deployment returns only tile metadata. Legacy deployments keep
  // the old delayed GeoJSON fallback during the compatibility window.
  const startupFortPromise = startupMapStatePromise.then(state => {
    if (state?.vector_tiles_enabled) return state.fortifications;
    return new Promise(resolve => {
      setTimeout(() => API.fetchFortifications().then(resolve), 0);
    });
  });
  const startupMapperPromise = Promise.all([startupMapStatePromise, startupMappersPromise]).then(async ([state, legacyIndex]) => {
    const index = state?.vector_tiles_enabled ? { mappers: state.mappers || [] } : legacyIndex;
    const selected = index?.mappers?.find(mapper => mapper.id === 'isw') || index?.mappers?.[0];
    const overlay = state?.vector_tiles_enabled
      ? selected
      : selected ? await API.fetchMapperOverlay(selected.id) : null;
    return { state, index, selected, overlay };
  });

  const hybridStylePromise = buildHybridStyle();
  const bootstrapStyle = {
    version: 8,
    glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
    sources: {},
    layers: [{ id: 'bootstrap-background', type: 'background', paint: { 'background-color': '#101214' } }],
  };

  const map = new maplibregl.Map({
    container: 'map',
    style: bootstrapStyle,
    center: [31.2, 48.5],
    zoom: 6,
  });

  function loadMapIcon(id, url) {
    if (map.hasImage(id)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => {
        if (!map.hasImage(id)) map.addImage(id, image);
        resolve();
      };
      image.onerror = () => reject(new Error(`Unable to load map icon: ${url}`));
      image.src = url;
    });
  }

  async function ensureMapIcons() {
    await Promise.all(Object.entries(MAP_ICONS).map(([id, url]) => loadMapIcon(id, url)));
  }

  function buildTileSwitcher() {
    const el = document.getElementById('tile-switcher');
    const groups = [...new Set(TILESETS.map(tileset => tileset.group))];
    el.className = 'basemap-control';
    el.innerHTML = `
      <div class="basemap-menu" id="basemap-menu" role="radiogroup" aria-label="Map background" hidden>
        <div class="basemap-menu-title">Map background</div>
        ${groups.map(group => `
          <div class="basemap-section">
            <div class="basemap-section-title">${group}</div>
            ${TILESETS.filter(tileset => tileset.group === group).map(tileset => `
              <button class="basemap-option ${tileset.id === _activeTilesetId ? 'active' : ''}"
                      type="button" role="radio" aria-checked="${tileset.id === _activeTilesetId}"
                      data-tile="${tileset.id}">
                <span class="basemap-radio"></span>
                <span>${tileset.label}</span>
              </button>
            `).join('')}
          </div>
        `).join('')}
      </div>
      <button class="basemap-toggle" type="button" aria-label="Choose map background"
              aria-controls="basemap-menu" aria-expanded="false" title="Map background">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="m12 3 8 5-8 5-8-5 8-5Z"></path>
          <path d="m4 12 8 5 8-5M4 16l8 5 8-5"></path>
        </svg>
      </button>`;

    const toggle = el.querySelector('.basemap-toggle');
    const menu = el.querySelector('.basemap-menu');
    const setOpen = open => {
      menu.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      el.classList.toggle('open', open);
    };

    toggle.addEventListener('click', event => {
      event.stopPropagation();
      setOpen(menu.hidden);
    });
    el.querySelectorAll('.basemap-option').forEach(option => {
      option.addEventListener('click', () => {
        setOpen(false);
        switchTileset(option.dataset.tile);
      });
    });
    document.addEventListener('click', event => {
      if (!el.contains(event.target)) setOpen(false);
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') setOpen(false);
    });
  }

  function buildMapperSwitcher() {
    const el = document.getElementById('mapper-switcher');
    el.className = 'switcher-group';
    el.innerHTML = _mappers.map(mapper =>
      `<button class="tile-btn ${mapper.id === _activeMapperId ? 'active' : ''}" data-mapper="${escHtml(mapper.id)}">${escHtml(mapper.display_name)}</button>`
    ).join('');
    el.querySelectorAll('[data-mapper]').forEach(btn => {
      btn.addEventListener('click', () => switchMapper(btn.dataset.mapper));
    });
  }

  function isApplicationLayer(id) {
    return id.startsWith('overlay-') || id.startsWith('fort-') ||
      id.startsWith('change-') || id.startsWith('cities-') ||
      id.startsWith('targets-') || id.startsWith('geo-');
  }

  function installBasemap(style) {
    // Basemap sources can spend a long time fetching tile metadata. Add them to
    // the existing lightweight style instead of calling setStyle(), which would
    // tear down the already-rendered application layers until all metadata loads.
    for (const id of [..._basemapLayerIds].reverse()) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    for (const id of _basemapSourceIds) {
      if (map.getSource(id)) map.removeSource(id);
    }
    _basemapLayerIds = [];
    _basemapSourceIds = [];

    for (const [id, source] of Object.entries(style.sources || {})) {
      if (map.getSource(id)) continue;
      map.addSource(id, source);
      _basemapSourceIds.push(id);
    }

    const before = (map.getStyle().layers || []).find(layer => isApplicationLayer(layer.id))?.id;
    for (const layer of style.layers || []) {
      if (map.getLayer(layer.id)) continue;
      map.addLayer(layer, before);
      _basemapLayerIds.push(layer.id);
    }
    if (map.getLayer('bootstrap-background')) map.removeLayer('bootstrap-background');
    setMarketCityBasemapLabels(_cityMap || { cities: {} });
    raiseMarkerLayers();
  }

  async function switchTileset(id) {
    if (id === _activeTilesetId) return;
    const tileset = TILESETS.find(entry => entry.id === id);
    if (!tileset) return;
    _activeTilesetId = id;
    document.querySelectorAll('#tile-switcher .basemap-option').forEach(option => {
      const active = option.dataset.tile === id;
      option.classList.toggle('active', active);
      option.setAttribute('aria-checked', String(active));
    });
    const style = await tileset.getStyle();
    installBasemap(style);
  }

  async function switchMapper(id) {
    if (!id) return;
    if (_changeComparison) exitChangeComparison();
    if (id === _activeMapperId && _overlayData?.mapper_id === id) return;
    if (_temporalMode) {
      const data = _mapState?.mappers?.find(mapper => mapper.id === id);
      if (!data) return;
      _activeMapperId = id;
      _overlayData = data;
      buildMapperSwitcher();
      renderMapperMeta(data);
      addOverlayLayers(data);
      renderLegend();
      return;
    }
    renderMapperMeta({ display_name: mapperDisplayName(id), status: 'loading' });
    const data = await API.fetchMapperOverlay(id);
    if (!data || data.error) {
      renderMapperMeta({ display_name: mapperDisplayName(id), status: 'error' });
      return;
    }
    _activeMapperId = id;
    _overlayData = data;
    buildMapperSwitcher();
    renderMapperMeta(data);
    addOverlayLayers(data);
    renderLegend();
  }

  function mapperDisplayName(id) {
    return _mappers.find(mapper => mapper.id === id)?.display_name || id || 'Overlay';
  }

  function renderMapperMeta(meta) {
    const el = document.getElementById('mapper-meta');
    if (!meta) {
      el.textContent = 'Overlay: unavailable';
      return;
    }
    if (meta.status === 'loading') {
      el.textContent = `${meta.display_name}: loading…`;
      return;
    }
    if (meta.status === 'unavailable' || meta.available === false) {
      el.innerHTML = `<span class="mapper-state-error">${escHtml(meta.display_name)}: unavailable for selected date</span>`;
      return;
    }
    const freshness = meta.last_updated ? `${meta.display_name}: updated ${relativeTime(meta.last_updated)}` : `${meta.display_name}: unavailable`;
    const stateClass = meta.status === 'stale' ? 'mapper-state-stale' : meta.status === 'error' ? 'mapper-state-error' : '';
    el.innerHTML = `<span class="${stateClass}">${escHtml(freshness)}</span>`;
  }

  function relativeTime(unixSeconds) {
    const seconds = Math.max(0, Math.round(Date.now() / 1000 - unixSeconds));
    if (seconds < 60) return `${seconds}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
    return `${Math.round(seconds / 86400)}d ago`;
  }

  function readdDataLayers() {
    if (_overlayData) addOverlayLayers(_overlayData);
    if (_geoData) addGeoLayers(_geoData);
    if (_cityMap) {
      addCityLayers(_cityMap);
      addTargetLayers(_cityMap);
    }
    if (_fortData) addFortLayers(_fortData);
    if (_changeComparison?.mode === 'changes') addChangeLayers(_changeComparison.detail);
  }

  function removeOverlayLayers() {
    for (const layerId of [..._overlayLayerIds].reverse()) {
      if (map.getLayer(layerId)) map.removeLayer(layerId);
    }
    for (const sourceId of _overlaySourceIds) {
      if (map.getSource(sourceId)) map.removeSource(sourceId);
    }
    _overlayLayerIds = [];
    _overlaySourceIds = [];
  }

  function addOverlayLayers(payload) {
    if (!map.getStyle()?.layers?.length) return;
    removeOverlayLayers();
    if (!payload?.available && payload?.tile_url !== undefined) return;
    if (payload.tile_url) {
      const sourceId = `overlay-${payload.mapper_id}-${payload.snapshot_id}`;
      map.addSource(sourceId, {
        type: 'vector',
        tiles: [absoluteTileUrl(payload.tile_url)],
        minzoom: 0,
        maxzoom: 14,
      });
      _overlaySourceIds.push(sourceId);
      for (const layer of payload.layers || []) {
        addOverlayLayerSet(sourceId, layer, `overlay-${payload.mapper_id}-${layer.id}`);
      }
      raiseMarkerLayers();
      return;
    }
    for (const layer of payload.layers || []) {
      const sourceId = `overlay-${payload.mapper_id}-${layer.id}`;
      map.addSource(sourceId, { type: 'geojson', data: layer.data });
      _overlaySourceIds.push(sourceId);
      addOverlayLayerSet(sourceId, layer, sourceId);
    }
    raiseMarkerLayers();
  }

  const MARKER_LAYER_IDS = [
    'geo-marker', 'geo-highlight',
    'cities-beacon-halo', 'cities-beacon-hover', 'cities-beacon',
    'targets-circle', 'targets-circle-hover',
    'cities-beacon-label',
    'geo-icon', 'cities-beacon-icon', 'targets-icon',
  ];

  function raiseMarkerLayers() {
    for (const id of MARKER_LAYER_IDS) {
      if (map.getLayer(id)) map.moveLayer(id);
    }
  }

  function _beforeCities() {
    const firstMarker = (map.getStyle().layers || []).find(layer => MARKER_LAYER_IDS.includes(layer.id));
    if (firstMarker) return firstMarker.id;
    return map.getLayer('cities-fill') ? 'cities-fill' : undefined;
  }

  function vectorLayerMetadata(layer) {
    return layer.source_layer ? {
      'source-layer': layer.source_layer,
      filter: ['==', ['get', 'layer_key'], layer.id],
    } : {};
  }

  function addOverlayLayerSet(sourceId, layer, stylePrefix) {
    const paint = layer.paint || {};
    const before = _beforeCities();
    const vector = vectorLayerMetadata(layer);
    if (layer.geom_type === 'polygon') {
      const fillId = `${stylePrefix}-fill`;
      const lineId = `${stylePrefix}-line`;
      map.addLayer({
        id: fillId,
        type: 'fill',
        source: sourceId,
        ...vector,
        paint: {
          'fill-color': ['coalesce', ['get', 'fill_color'], paint.fill_color || '#999999'],
          'fill-opacity': ['coalesce', ['get', 'fill_opacity'], paint.fill_opacity ?? 0.25],
        },
      }, before);
      map.addLayer({
        id: lineId,
        type: 'line',
        source: sourceId,
        ...vector,
        paint: {
          'line-color': ['coalesce', ['get', 'line_color'], paint.line_color || paint.fill_color || '#999999'],
          'line-opacity': ['coalesce', ['get', 'line_opacity'], paint.line_opacity ?? 1],
          'line-width': ['coalesce', ['get', 'line_width'], paint.line_width ?? 1.5],
        },
      }, before);
      _overlayLayerIds.push(fillId, lineId);
      return;
    }

    if (layer.geom_type === 'line') {
      const lineId = `${stylePrefix}-line`;
      map.addLayer({
        id: lineId,
        type: 'line',
        source: sourceId,
        ...vector,
        paint: {
          'line-color': ['coalesce', ['get', 'line_color'], paint.line_color || '#999999'],
          'line-opacity': ['coalesce', ['get', 'line_opacity'], paint.line_opacity ?? 1],
          'line-width': ['coalesce', ['get', 'line_width'], paint.line_width ?? 1.5],
        },
      }, before);
      _overlayLayerIds.push(lineId);
      return;
    }

    const circleId = `${stylePrefix}-circle`;
    map.addLayer({
      id: circleId,
      type: 'circle',
      source: sourceId,
      ...vector,
      paint: {
        'circle-color': ['coalesce', ['get', 'circle_color'], paint.circle_color || '#999999'],
        'circle-opacity': ['coalesce', ['get', 'circle_opacity'], paint.circle_opacity ?? 1],
        'circle-radius': ['coalesce', ['get', 'circle_radius'], paint.circle_radius ?? 4],
        'circle-stroke-color': paint.circle_stroke_color || '#ffffff',
        'circle-stroke-width': paint.circle_stroke_width ?? 1.5,
      },
    }, before);
    _overlayLayerIds.push(circleId);
  }

  function addFortLayerSet(sourceId, layer, stylePrefix) {
    const paint = layer.paint || {};
    const before = _beforeCities();
    const layout = { visibility: _fortLayerVisibility.get(layer.id) === false ? 'none' : 'visible' };
    const vector = vectorLayerMetadata(layer);
    if (layer.geom_type === 'polygon') {
      const fillId = `${stylePrefix}-fill`;
      const lineId = `${stylePrefix}-line`;
      map.addLayer({ id: fillId, type: 'fill', source: sourceId, ...vector, layout, paint: { 'fill-color': paint.fill_color || '#aaaaaa', 'fill-opacity': paint.fill_opacity ?? 0.3 } }, before);
      map.addLayer({ id: lineId, type: 'line', source: sourceId, ...vector, layout, paint: { 'line-color': paint.line_color || '#cccccc', 'line-opacity': paint.line_opacity ?? 0.85, 'line-width': paint.line_width ?? 1.5 } }, before);
      _fortLayerIds.push(fillId, lineId);
      _fortStyleLayersByDataId.set(layer.id, [fillId, lineId]);
      return;
    }
    if (layer.geom_type === 'line') {
      const lineId = `${stylePrefix}-line`;
      map.addLayer({ id: lineId, type: 'line', source: sourceId, ...vector, layout, paint: { 'line-color': paint.line_color || '#cccccc', 'line-opacity': paint.line_opacity ?? 0.85, 'line-width': paint.line_width ?? 1.5 } }, before);
      _fortLayerIds.push(lineId);
      _fortStyleLayersByDataId.set(layer.id, [lineId]);
      return;
    }
    const circleId = `${stylePrefix}-circle`;
    map.addLayer({ id: circleId, type: 'circle', source: sourceId, ...vector, layout, paint: { 'circle-color': paint.circle_color || '#cccccc', 'circle-opacity': paint.circle_opacity ?? 0.85, 'circle-radius': paint.circle_radius ?? 4, 'circle-stroke-color': paint.circle_stroke_color || '#ffffff', 'circle-stroke-width': paint.circle_stroke_width ?? 1.5 } }, before);
    _fortLayerIds.push(circleId);
    _fortStyleLayersByDataId.set(layer.id, [circleId]);
  }

  function addFortLayers(data) {
    for (const id of [..._fortLayerIds].reverse())
      if (map.getLayer(id)) map.removeLayer(id);
    for (const id of _fortSourceIds)
      if (map.getSource(id)) map.removeSource(id);
    _fortLayerIds = [];
    _fortSourceIds = [];
    _fortStyleLayersByDataId.clear();

    if (!data) return;
    if (!data?.available && data?.tile_url !== undefined) return;
    if (data.tile_url) {
      const sourceId = `fort-${data.snapshot_id}`;
      map.addSource(sourceId, {
        type: 'vector',
        tiles: [absoluteTileUrl(data.tile_url)],
        minzoom: 0,
        maxzoom: 14,
      });
      _fortSourceIds.push(sourceId);
      for (const layer of data.layers || []) {
        if (!_fortLayerVisibility.has(layer.id)) _fortLayerVisibility.set(layer.id, true);
        addFortLayerSet(sourceId, layer, `fort-${layer.id}`);
      }
      raiseMarkerLayers();
      return;
    }

    for (const layer of data.layers || []) {
      if (!_fortLayerVisibility.has(layer.id)) _fortLayerVisibility.set(layer.id, true);
      const sourceId = `fort-${layer.id}`;
      map.addSource(sourceId, { type: 'geojson', data: layer.data });
      _fortSourceIds.push(sourceId);
      addFortLayerSet(sourceId, layer, sourceId);
    }
    raiseMarkerLayers();
  }

  function setFortLayerVisibility(dataLayerId, visible) {
    _fortLayerVisibility.set(dataLayerId, visible);
    for (const layerId of _fortStyleLayersByDataId.get(dataLayerId) || []) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, 'visibility', visible ? 'visible' : 'none');
      }
    }
  }

  function removeChangeLayers() {
    for (const id of [..._changeLayerIds].reverse()) {
      if (map.getLayer(id)) map.removeLayer(id);
    }
    if (_changeSourceId && map.getSource(_changeSourceId)) map.removeSource(_changeSourceId);
    _changeLayerIds = [];
    _changeSourceId = null;
  }

  function changeHatchId(color) {
    return `change-hatch-${String(color).replace(/[^0-9a-f]/gi, '').toLowerCase() || '999999'}`;
  }

  function addChangeHatch(color) {
    const id = changeHatchId(color);
    if (map.hasImage(id)) return id;
    let hex = String(color || '#999999').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(value => value + value).join('');
    if (!/^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(hex)) hex = '999999';
    const red = parseInt(hex.slice(0, 2), 16);
    const green = parseInt(hex.slice(2, 4), 16);
    const blue = parseInt(hex.slice(4, 6), 16);
    const size = 32;
    const data = new Uint8Array(size * size * 4);
    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) {
        const offset = (y * size + x) * 4;
        const stripe = ((x + y) % 16) < 5;
        data[offset] = red;
        data[offset + 1] = green;
        data[offset + 2] = blue;
        data[offset + 3] = stripe ? 235 : 0;
      }
    }
    map.addImage(id, { width: size, height: size, data }, { pixelRatio: 2 });
    return id;
  }

  function removedHatchExpression(detail) {
    const colors = [...new Set([...(detail?.removed_fill_colors || []), '#999999'])];
    const pairs = [];
    for (const color of colors) pairs.push(color, ['image', addChangeHatch(color)]);
    const fallback = addChangeHatch('#999999');
    return ['match', ['coalesce', ['get', 'fill_color'], '#999999'], ...pairs, ['image', fallback]];
  }

  function addChangeLayers(detail) {
    removeChangeLayers();
    if (!detail?.change_tile_url) return;
    _changeSourceId = `change-${detail.id}`;
    map.addSource(_changeSourceId, {
      type: 'vector', tiles: [absoluteTileUrl(detail.change_tile_url)], minzoom: 0, maxzoom: 14,
    });
    const variants = [
      { key: 'added-after', type: 'added', phase: 'after', indicator: '#54e383', dash: [0.6, 1.5], restore: false },
      { key: 'removed-before', type: 'removed', phase: 'before', indicator: '#ff6868', dash: [3, 2], restore: true },
      { key: 'modified-before', type: 'modified', phase: 'before', indicator: '#ff6868', dash: [3, 2], restore: true },
      { key: 'modified-after', type: 'modified', phase: 'after', indicator: '#54e383', dash: [0.6, 1.5], restore: false },
      { key: 'modified-style', type: 'modified', phase: 'style', indicator: '#f0c75e', dash: [2, 2], restore: false },
    ];
    const before = _beforeCities();
    const removedPattern = removedHatchExpression(detail);
    for (const variant of variants) {
      const commonFilter = [['==', ['get', 'change_type'], variant.type], ['==', ['get', 'phase'], variant.phase]];
      if (variant.restore) {
        const fillId = `change-${variant.key}-original-fill`;
        const lineId = `change-${variant.key}-original-line`;
        map.addLayer({
          id: fillId, type: 'fill', source: _changeSourceId, 'source-layer': 'changes',
          filter: ['all', ...commonFilter, ['==', ['geometry-type'], 'Polygon']],
          paint: {
            'fill-pattern': removedPattern,
            'fill-opacity': 1,
          },
        }, before);
        map.addLayer({
          id: lineId, type: 'line', source: _changeSourceId, 'source-layer': 'changes',
          filter: ['all', ...commonFilter, ['!=', ['geometry-type'], 'Point']],
          paint: {
            'line-color': ['coalesce', ['get', 'line_color'], '#999999'],
            'line-opacity': 0.35,
            'line-width': ['+', ['coalesce', ['get', 'line_width'], 1.5], 1],
          },
        }, before);
        _changeLayerIds.push(fillId, lineId);
      }

      const outlineId = `change-${variant.key}-outline`;
      const pointId = `change-${variant.key}-point`;
      map.addLayer({
        id: outlineId, type: 'line', source: _changeSourceId, 'source-layer': 'changes',
        filter: ['all', ...commonFilter, ['!=', ['geometry-type'], 'Point']],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-color': variant.indicator,
          'line-width': 3,
          'line-opacity': 1,
          'line-dasharray': variant.dash,
        },
      }, before);
      map.addLayer({
        id: pointId, type: 'circle', source: _changeSourceId, 'source-layer': 'changes',
        filter: ['all', ...commonFilter, ['==', ['geometry-type'], 'Point']],
        paint: {
          'circle-color': variant.restore
            ? ['coalesce', ['get', 'circle_color'], ['get', 'fill_color'], '#999999']
            : 'rgba(0,0,0,0)',
          'circle-opacity': variant.restore ? 0.4 : 1,
          'circle-radius': ['+', ['coalesce', ['get', 'circle_radius'], 4], 2],
          'circle-stroke-color': variant.indicator,
          'circle-stroke-width': 3,
        },
      }, before);
      _changeLayerIds.push(outlineId, pointId);
    }
    raiseMarkerLayers();
  }

  function comparisonControl() {
    let control = document.getElementById('map-change-comparison');
    if (control) return control;
    control = document.createElement('div');
    control.id = 'map-change-comparison';
    control.hidden = true;
    control.innerHTML = `<div class="map-change-comparison-meta"></div>
      <div class="map-change-comparison-legend"><span class="added">Added</span><span class="removed">Removed</span><span class="style">Style only</span></div>
      <div class="map-change-comparison-modes" role="group" aria-label="Map change comparison">
        <button type="button" data-change-mode="before">Before</button>
        <button type="button" data-change-mode="changes">Changes</button>
        <button type="button" data-change-mode="after">After</button>
      </div>
      <button class="map-change-comparison-close" type="button" aria-label="Close map comparison">×</button>`;
    document.getElementById('app').appendChild(control);
    control.querySelectorAll('[data-change-mode]').forEach(button => {
      button.addEventListener('click', () => renderChangeComparison(button.dataset.changeMode));
    });
    control.querySelector('.map-change-comparison-close').addEventListener('click', exitChangeComparison);
    return control;
  }

  function comparisonPayload(detail, snapshot) {
    return {
      ...snapshot,
      id: detail.source.id,
      mapper_id: detail.source.id,
      kind: detail.source.kind,
      display_name: detail.source.display_name,
      available: true,
      snapshot_id: snapshot.id,
      last_updated: Date.parse(detail.observed_at) / 1000,
      status: 'ok',
    };
  }

  function renderChangeComparison(mode = 'changes') {
    if (!_changeComparison) return;
    const detail = _changeComparison.detail;
    _changeComparison.mode = mode;
    const snapshot = mode === 'before' ? detail.before : detail.after;
    if (!snapshot) return;
    const payload = comparisonPayload(detail, snapshot);
    if (detail.source.kind === 'fortifications') {
      _fortData = payload;
      addFortLayers(payload);
    } else {
      _activeMapperId = detail.source.id;
      _overlayData = payload;
      buildMapperSwitcher();
      addOverlayLayers(payload);
      renderMapperMeta(payload);
    }
    if (mode === 'changes') addChangeLayers(detail);
    else removeChangeLayers();
    const control = comparisonControl();
    control.hidden = false;
    control.querySelector('.map-change-comparison-meta').textContent = `${detail.source.display_name} · ${formatComparisonTime(detail.observed_at)} Kyiv`;
    control.querySelectorAll('[data-change-mode]').forEach(button => {
      button.classList.toggle('active', button.dataset.changeMode === mode);
    });
    renderLegend();
  }

  function formatComparisonTime(raw) {
    return new Intl.DateTimeFormat('en-GB', {
      day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
      timeZone: 'Europe/Kyiv', hour12: false,
    }).format(new Date(raw));
  }

  function showChangeComparison(detail) {
    if (!detail?.before || !detail?.after) return;
    if (_changeComparison) exitChangeComparison();
    _changeComparison = { detail, mode: 'changes', returnMapperId: _activeMapperId };
    renderChangeComparison('changes');
    const [west, south, east, north] = detail.bounds;
    const lonPad = Math.max((east - west) * 0.16, 0.05);
    const latPad = Math.max((north - south) * 0.16, 0.04);
    map.fitBounds([[west - lonPad, south - latPad], [east + lonPad, north + latPad]], {
      padding: { top: 80, right: 40, bottom: 100, left: 40 },
      maxZoom: 12, duration: 900, essential: true,
    });
  }

  function exitChangeComparison() {
    if (!_changeComparison) return;
    const returnMapperId = _changeComparison.returnMapperId;
    _changeComparison = null;
    removeChangeLayers();
    const control = document.getElementById('map-change-comparison');
    if (control) control.hidden = true;
    if (_mapState) {
      _activeMapperId = returnMapperId;
      applyTemporalMapState(_mapState, { forceGeometry: true });
    }
  }

  function isActiveMarket(market) {
    return market.status ? market.status === 'active' : market.active !== false;
  }

  function priority(markets) {
    if ((markets.capture_all || []).some(isActiveMarket)) return 'capture_all';
    if ((markets.capture || []).some(isActiveMarket)) return 'capture';
    if ((markets.enter || []).some(isActiveMarket)) return 'enter';
    return null;
  }

  function setMarketCityBasemapLabels(cityMap) {
    const names = new Set();
    for (const entry of Object.values(cityMap.cities || {})) {
      const hasActiveMarket = Object.values(entry.markets || {})
        .flat()
        .some(isActiveMarket);
      if (!hasActiveMarket) continue;

      const name = (entry.city || {}).name_en;
      if (!name) continue;
      names.add(name);
      names.add(name.split(' (')[0].trim());
      const osmAlias = name.match(/OSM labels as ([^)]+)/i);
      if (osmAlias) names.add(osmAlias[1].trim());
    }
    _marketCityNames = [...names];

    for (const layer of map.getStyle().layers || []) {
      if (layer['source-layer'] !== 'place' || !map.getLayer(layer.id)) continue;
      const baseFilter = layer.metadata?.['wardotfun:base-filter'];
      map.setFilter(layer.id, marketCityBasemapFilter(baseFilter));
    }
  }

  async function addGeoLayers(data) {
    for (const id of ['geo-icon', 'geo-highlight', 'geo-marker', 'geo-cluster-count', 'geo-clusters']) if (map.getLayer(id)) map.removeLayer(id);
    if (map.getSource('geolocations')) map.removeSource('geolocations');
    map.addSource('geolocations', { type: 'geojson', data: geolocationFeatureCollection(data) });
    map.addLayer({ id: 'geo-marker', type: 'circle', source: 'geolocations', paint: {
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 8, 9, 10, 13, 13],
      'circle-color': ['get', 'faction_color'], 'circle-opacity': 0.96,
      'circle-stroke-width': 2, 'circle-stroke-color': '#111111',
    }});
    map.addLayer({ id: 'geo-highlight', type: 'circle', source: 'geolocations', filter: ['==', ['get', 'uuid'], ''], paint: {
      'circle-radius': 18, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 3,
    }});
    if (!_geoEventsBound) {
      map.on('click', 'geo-marker', _onGeoClick);
      map.on('mouseenter', 'geo-marker', () => { map.getCanvas().style.cursor = 'pointer'; });
      map.on('mouseleave', 'geo-marker', () => { map.getCanvas().style.cursor = ''; });
      _geoEventsBound = true;
    }

    // MapLibre resolves symbol images when the layer is created. Register every
    // GeoConfirmed image first so a slow icon response cannot leave an empty layer.
    await ensureGeoIcons(data);
    if (!map.getSource('geolocations') || map.getLayer('geo-icon')) return;
    map.addLayer({ id: 'geo-icon', type: 'symbol', source: 'geolocations', layout: {
      'icon-image': ['get', 'icon_key'], 'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.46, 9, 0.55, 13, 0.72],
      'icon-allow-overlap': true, 'icon-ignore-placement': true,
    }});
    map.on('mouseenter', 'geo-icon', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'geo-icon', () => { map.getCanvas().style.cursor = ''; });
    raiseMarkerLayers();
  }

  function ensureGeoIcons(data) {
    const icons = new Map((data?.events || [])
      .filter(event => event.icon_id && event.icon_url)
      .map(event => [`geo-${event.icon_id}`, event.icon_url]));
    return Promise.all([...icons].map(([id, url]) => loadMapIcon(id, url).catch(error => {
      console.warn(error);
      return null;
    })));
  }
  function geolocationFeatureCollection(data) {
    return {
      type: 'FeatureCollection',
      features: (data?.events || []).map(event => ({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [event.lon, event.lat] },
        properties: {
          uuid: event.uuid,
          faction_color: event.faction_color || "#666666",
          icon_key: event.icon_id ? `geo-${event.icon_id}` : "geolocation-icon",
        },
      })),
    };
  }

  function applyGeolocationData(data) {
    if (!data || !Array.isArray(data.events)) return;
    _geoData = data;
    if (!_temporalMode) _geoDates = [...new Set(data.dates || [])].sort();
    _geoDate = data.date || _geoDates.at(-1) || null;
    if (map.getSource('geolocations')) {
      map.getSource('geolocations').setData(geolocationFeatureCollection(data));
    } else if (map.isStyleLoaded()) {
      addGeoLayers(data);
    }
    ensureGeoIcons(data);
    if (window.MarketDrawer) window.MarketDrawer.setGeolocations(data);
    renderGeoTimeline();
    renderLegend();
  }

  function updateMapChangesContext(state = _mapState) {
    if (!window.MarketDrawer?.setMapChangesContext) return;
    const sources = [...(state?.mappers || [])];
    if (state?.fortifications) sources.push(state.fortifications);
    window.MarketDrawer.setMapChangesContext({
      enabled: Boolean(state?.map_changes_enabled),
      date: state?.date || _geoDate,
      sources,
    });
  }

  function applyTemporalMapState(state, { forceGeometry = false } = {}) {
    if (!state?.vector_tiles_enabled) return false;
    const previousOverlayKey = `${_overlayData?.mapper_id || ''}:${_overlayData?.snapshot_id || ''}`;
    const previousFortKey = `${_fortData?.id || ''}:${_fortData?.snapshot_id || ''}`;
    _temporalMode = true;
    _mapState = state;
    _geoDates = [...new Set(state.available_dates || [])].sort();
    _geoDate = state.date;
    _mappers = state.mappers || [];
    if (!_mappers.some(mapper => mapper.id === _activeMapperId)) {
      _activeMapperId = (_mappers.find(mapper => mapper.id === 'isw') || _mappers[0])?.id || null;
    }
    const nextOverlay = _mappers.find(mapper => mapper.id === _activeMapperId) || null;
    const nextOverlayKey = `${nextOverlay?.mapper_id || ''}:${nextOverlay?.snapshot_id || ''}`;
    const nextFort = state.fortifications || null;
    const nextFortKey = `${nextFort?.id || ''}:${nextFort?.snapshot_id || ''}`;
    _overlayData = nextOverlay;
    _fortData = nextFort;
    updateMapChangesContext(state);
    buildMapperSwitcher();
    renderMapperMeta(nextOverlay);
    if (map.isStyleLoaded()) {
      if (forceGeometry || previousOverlayKey !== nextOverlayKey) {
        if (nextOverlay) addOverlayLayers(nextOverlay);
        else removeOverlayLayers();
      }
      if (forceGeometry || previousFortKey !== nextFortKey) addFortLayers(nextFort);
    }
    renderGeoTimeline();
    renderLegend();
    return true;
  }

  async function selectGeoDate(date) {
    if (_changeComparison) exitChangeComparison();
    if (!date || date === _geoDate || !_geoDates.includes(date)) return;
    const requestSerial = ++_geoRequestSerial;
    let failed = false;
    setGeoTimelineLoading(true);
    try {
      const [state, data] = await Promise.all([
        _temporalMode ? API.fetchMapState(date) : Promise.resolve(null),
        API.fetchGeolocations(date),
      ]);
      if (requestSerial !== _geoRequestSerial) return;
      if (_temporalMode && !applyTemporalMapState(state)) throw new Error('Temporal map state unavailable');
      if (!Array.isArray(data?.events)) throw new Error('GeoConfirmed data unavailable');
      applyGeolocationData(data);
    } catch (error) {
      if (requestSerial !== _geoRequestSerial) return;
      failed = true;
      console.warn(`Unable to load geolocations for ${date}`, error);
    } finally {
      if (requestSerial === _geoRequestSerial) {
        setGeoTimelineLoading(false);
        if (failed) renderGeoTimeline(true);
      }
    }
  }

  function compactDateToInput(date) {
    return date?.length === 8 ? `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6)}` : '';
  }

  function renderGeoTimeline(hasError = false) {
    const timeline = document.getElementById('geo-timeline');
    if (!timeline || !_geoDate) {
      if (timeline) timeline.hidden = true;
      return;
    }
    timeline.hidden = false;
    timeline.classList.toggle('error', hasError);
    const index = _geoDates.indexOf(_geoDate);
    const [year, month, day] = [_geoDate.slice(0, 4), _geoDate.slice(4, 6), _geoDate.slice(6, 8)];
    timeline.querySelector('.geo-timeline-date').textContent = `${day}.${month}.${year}`;
    timeline.querySelector('.geo-timeline-input').value = compactDateToInput(_geoDate);
    for (const button of timeline.querySelectorAll('[data-geo-step]')) {
      const nextIndex = index + Number(button.dataset.geoStep);
      button.disabled = index < 0 || nextIndex < 0 || nextIndex >= _geoDates.length;
    }
  }

  function setGeoTimelineLoading(loading) {
    const timeline = document.getElementById('geo-timeline');
    if (!timeline) return;
    timeline.classList.toggle('loading', loading);
    for (const button of timeline.querySelectorAll('button')) button.disabled = loading;
    if (!loading) renderGeoTimeline();
  }

  function buildGeoTimeline() {
    const timeline = document.getElementById('geo-timeline');
    const input = timeline?.querySelector('.geo-timeline-input');
    if (!timeline || !input) return;
    for (const button of timeline.querySelectorAll('[data-geo-step]')) {
      button.addEventListener('click', () => {
        const index = _geoDates.indexOf(_geoDate) + Number(button.dataset.geoStep);
        selectGeoDate(_geoDates[index]);
      });
    }
    const openCalendar = () => {
      input.min = compactDateToInput(_geoDates[0]);
      input.max = compactDateToInput(_geoDates.at(-1));
      try {
        if (typeof input.showPicker === 'function') input.showPicker();
        else input.click();
      } catch (_error) {
        input.click();
      }
    };
    timeline.querySelector('.geo-timeline-date').addEventListener('click', openCalendar);
    timeline.querySelector('.geo-timeline-calendar').addEventListener('click', openCalendar);
    input.addEventListener('change', () => {
      const date = input.value.replaceAll('-', '');
      if (_geoDates.includes(date)) selectGeoDate(date);
      else renderGeoTimeline(true);
    });
  }

  function _onGeoClick(e) {
    if (!e.features.length) return;
    window.MarketDrawer.openGeolocation(e.features[0].properties.uuid);
  }

  function locateGeoEvent(event) {
    map.flyTo({ center: [event.lon, event.lat], zoom: Math.max(map.getZoom(), 12), duration: 900, essential: true });
    if (map.getLayer('geo-highlight')) map.setFilter('geo-highlight', ['==', ['get', 'uuid'], event.uuid]);
    setTimeout(() => {
      if (map.getLayer('geo-highlight')) map.setFilter('geo-highlight', ['==', ['get', 'uuid'], '']);
    }, 1800);
  }

  function extractSourceUrls(value) {
    return String(value || '')
      .match(/https?:\/\/[^\s]+/g)
      ?.map(url => url.replace(/[),.;]+$/, '')) || [];
  }

  function twitterStatus(url) {
    try {
      const parsed = new URL(url);
      const hostname = parsed.hostname.toLowerCase().replace(/^www\./, '');
      if (hostname !== 'x.com' && hostname !== 'twitter.com' && hostname !== 'mobile.twitter.com')
        return null;
      const match = parsed.pathname.match(/\/status\/(\d+)/);
      return match ? { id: match[1], url } : null;
    } catch (_error) {
      return null;
    }
  }

  function telegramPost(url) {
    try {
      const parsed = new URL(url);
      const hostname = parsed.hostname.toLowerCase().replace(/^www\./, '');
      if (hostname !== 't.me' && hostname !== 'telegram.me') return null;
      const parts = parsed.pathname.split('/').filter(Boolean);
      if (parts[0] === 's') parts.shift();
      if (parts.length !== 2 || parts[0] === 'c' || !/^\d+$/.test(parts[1])) return null;
      return { post: `${parts[0]}/${parts[1]}`, url };
    } catch (_error) {
      return null;
    }
  }

  function geoSourceHTML(value) {
    const urls = [...new Set(extractSourceUrls(value))];
    if (!urls.length) return '';

    const embeds = urls.map(url => {
      const tweet = twitterStatus(url);
      if (tweet) return { type: 'twitter', ...tweet };
      const telegram = telegramPost(url);
      return telegram ? { type: 'telegram', ...telegram } : null;
    }).filter(Boolean);
    const embeddedUrls = new Set(embeds.map(embed => embed.url));
    const links = urls.filter(url => !embeddedUrls.has(url));
    const embedsHtml = embeds.length
      ? `<div class="geo-popup-embeds">${embeds.map(embed => embed.type === 'twitter' ? `
          <div class="geo-popup-tweet" data-twitter-status="${embed.id}" data-twitter-url="${escHtml(embed.url)}">
            <span class="geo-popup-embed-loading">Loading post...</span>
          </div>` : `
          <div class="geo-popup-telegram" data-telegram-post="${escHtml(embed.post)}" data-telegram-url="${escHtml(embed.url)}">
            <span class="geo-popup-embed-loading">Loading post...</span>
          </div>`).join('')}</div>`
      : '';
    const linksHtml = links.map(url =>
      `<a href="${escHtml(url)}" target="_blank" rel="noopener" class="geo-popup-link">Open source ↗</a>`
    ).join('');
    return embedsHtml + linksHtml;
  }

  function loadTwitterWidgets() {
    if (window.twttr?.widgets?.createTweet) return Promise.resolve(window.twttr);
    if (_twitterWidgetsPromise) return _twitterWidgetsPromise;

    _twitterWidgetsPromise = new Promise((resolve, reject) => {
      const scriptUrl = 'https://platform.twitter.com/widgets.js';
      let script = document.querySelector(`script[src="${scriptUrl}"]`);
      const timeout = setTimeout(() => reject(new Error('Twitter widgets timed out')), 12_000);
      const ready = () => {
        if (!window.twttr?.widgets?.createTweet) {
          clearTimeout(timeout);
          reject(new Error('Twitter widgets unavailable'));
          return;
        }
        window.twttr.ready(() => {
          clearTimeout(timeout);
          resolve(window.twttr);
        });
      };
      const failed = () => {
        clearTimeout(timeout);
        reject(new Error('Twitter widgets failed to load'));
      };

      if (script) {
        script.addEventListener('load', ready, { once: true });
        script.addEventListener('error', failed, { once: true });
        return;
      }
      script = document.createElement('script');
      script.src = scriptUrl;
      script.async = true;
      script.addEventListener('load', ready, { once: true });
      script.addEventListener('error', failed, { once: true });
      document.head.appendChild(script);
    });
    return _twitterWidgetsPromise;
  }

  async function renderTwitterEmbed(container) {
    const statusId = container.dataset.twitterStatus;
    const sourceUrl = container.dataset.twitterUrl;
    try {
      const twitter = await loadTwitterWidgets();
      if (!container.isConnected) return;
      container.replaceChildren();
      const tweet = await twitter.widgets.createTweet(statusId, container, {
        theme: 'dark',
        dnt: true,
        align: 'center',
        conversation: 'none',
      });
      if (!tweet) throw new Error('Post is unavailable');
    } catch (_error) {
      if (!container.isConnected) return;
      container.classList.add('failed');
      container.innerHTML = `<a href="${escHtml(sourceUrl)}" target="_blank" rel="noopener" class="geo-popup-link">Open post on X ↗</a>`;
    }
  }

  function renderTelegramEmbed(container) {
    const sourceUrl = container.dataset.telegramUrl;
    const fallback = () => {
      observer.disconnect();
      clearTimeout(timeout);
      if (!container.isConnected) return;
      container.classList.add('failed');
      container.innerHTML = `<a href="${escHtml(sourceUrl)}" target="_blank" rel="noopener" class="geo-popup-link">Open post on Telegram ↗</a>`;
    };
    const observer = new MutationObserver(() => {
      if (!container.querySelector('iframe')) return;
      observer.disconnect();
      clearTimeout(timeout);
    });
    const timeout = setTimeout(fallback, 12_000);
    const script = document.createElement('script');
    script.src = 'https://telegram.org/js/telegram-widget.js?22';
    script.async = true;
    script.dataset.telegramPost = container.dataset.telegramPost;
    script.dataset.width = '100%';
    script.dataset.dark = '1';
    script.addEventListener('error', fallback, { once: true });
    container.replaceChildren();
    observer.observe(container, { childList: true, subtree: true });
    container.appendChild(script);
  }

  function addCityLayers(cityMap) {
    _cityEntries = {};
    const polygonFeatures = [];
    const beaconFeatures = [];
    for (const [cityId, entry] of Object.entries(cityMap.cities || {})) {
      const p = priority(entry.markets || {});
      if (!p || !entry.geometry) continue;
      _cityEntries[cityId] = entry;
      const activeMarkets = Object.values(entry.markets || {})
        .flat()
        .filter(isActiveMarket);
      const properties = {
        cityId,
        name: (entry.city || {}).name_en || 'Unknown',
        marketType: p,
        marketCount: activeMarkets.length,
        priorityRank: p === 'capture_all' ? 0 : p === 'capture' ? 1 : 2,
        color: CITY_COLORS[p],
      };
      polygonFeatures.push({
        type: 'Feature',
        id: cityId,
        geometry: entry.geometry,
        properties,
      });
      if (entry.marker) {
        beaconFeatures.push({
          type: 'Feature',
          id: cityId,
          geometry: entry.marker,
          properties,
        });
      }
    }

    const polygons = { type: 'FeatureCollection', features: polygonFeatures };
    const beacons = { type: 'FeatureCollection', features: beaconFeatures };

    if (map.getSource('cities')) {
      map.getSource('cities').setData(polygons);
      if (map.getSource('city-beacons')) map.getSource('city-beacons').setData(beacons);
      raiseMarkerLayers();
      return;
    }

    map.addSource('cities', { type: 'geojson', data: polygons });
    map.addSource('city-beacons', { type: 'geojson', data: beacons });
    map.addLayer({ id: 'cities-fill', type: 'fill', source: 'cities', paint: { 'fill-color': '#d8d0cf', 'fill-opacity': 0.09 } });
    map.addLayer({
      id: 'cities-line',
      type: 'line',
      source: 'cities',
      paint: {
        'line-color': '#f5f2ec',
        'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1, 10, 1.5],
        'line-opacity': 0.9,
      },
    });
    map.addLayer({ id: 'cities-fill-hover', type: 'fill', source: 'cities', paint: { 'fill-color': '#ffffff', 'fill-opacity': 0.16 }, filter: ['==', ['get', 'cityId'], ''] });
    map.addLayer({
      id: 'cities-beacon-halo',
      type: 'circle',
      source: 'city-beacons',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'],
          5, ['match', ['get', 'marketType'], 'capture_all', 9, 'capture', 8, 7],
          10, ['match', ['get', 'marketType'], 'capture_all', 12, 'capture', 11, 10],
        ],
        'circle-color': '#090909',
        'circle-opacity': 0.9,
      },
    });
    map.addLayer({
      id: 'cities-beacon-hover',
      type: 'circle',
      source: 'city-beacons',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'], 5, 11, 10, 15],
        'circle-color': ['get', 'color'],
        'circle-opacity': 0.28,
        'circle-stroke-color': '#ffffff',
        'circle-stroke-width': 1,
      },
      filter: ['==', ['get', 'cityId'], ''],
    });
    map.addLayer({
      id: 'cities-beacon',
      type: 'circle',
      source: 'city-beacons',
      paint: {
        'circle-radius': ['interpolate', ['linear'], ['zoom'],
          5, ['match', ['get', 'marketType'], 'capture_all', 6.5, 'capture', 6, 5.5],
          10, ['match', ['get', 'marketType'], 'capture_all', 9, 'capture', 8, 7],
        ],
        'circle-color': '#ffffff',
        'circle-opacity': 1,
        'circle-stroke-color': ['match', ['get', 'marketType'], 'enter', '#b59f3b', '#cc3333'],
        'circle-stroke-width': 3,
      },
    });
    map.addLayer({
      id: 'cities-beacon-icon',
      type: 'symbol',
      source: 'city-beacons',
      layout: {
        'icon-image': 'market-city-icon',
        'icon-size': ['interpolate', ['linear'], ['zoom'], 5, 0.4, 10, 0.56],
        'icon-allow-overlap': true,
        'icon-ignore-placement': true,
        'symbol-sort-key': ['get', 'priorityRank'],
      },
    });
    map.addLayer({
      id: 'cities-beacon-label',
      type: 'symbol',
      source: 'city-beacons',
      minzoom: 5,
      layout: {
        'text-field': ['get', 'name'],
        'text-font': ['Noto Sans Regular'],
        'text-size': ['interpolate', ['linear'], ['zoom'], 5, 10, 11, 14],
        'text-anchor': 'top',
        'text-offset': [0, 1.25],
        'text-optional': true,
        'symbol-sort-key': ['get', 'priorityRank'],
      },
      paint: {
        'text-color': '#ffffff',
        'text-halo-color': '#111111',
        'text-halo-width': 2,
        'text-halo-blur': 0.5,
      },
    });
    raiseMarkerLayers();
  }

  function addTargetLayers(cityMap) {
    _targetEntries = {};
    const features = [];

    for (const [cityId, entry] of Object.entries(cityMap.cities || {})) {
      const captureMarkets = (entry.markets?.capture || []).filter(market => market.target && isActiveMarket(market));
      if (!captureMarkets.length) continue;

      const byObjective = {};
      for (const market of captureMarkets) {
        const coordKey = `${market.target.lon},${market.target.lat}`;
        const key = market.eventSlug || coordKey;
        if (!byObjective[key]) byObjective[key] = { coordKey, markets: [] };
        byObjective[key].markets.push(market);
      }

      for (const [objectiveKey, objective] of Object.entries(byObjective)) {
        const { coordKey, markets } = objective;
        const [lon, lat] = coordKey.split(',').map(Number);
        const targetKey = `${cityId}::${objectiveKey}`;
        const targetLabel = markets[0].target?.label || 'Capture target';
        _targetEntries[targetKey] = {
          cityId,
          cityEntry: { ...entry, markets: { enter: [], capture: markets, capture_all: [] } },
          lngLat: [lon, lat],
        };
        features.push({
          type: 'Feature',
          properties: { targetKey, cityName: (entry.city || {}).name_en || 'Unknown', targetLabel },
          geometry: { type: 'Point', coordinates: [lon, lat] },
        });
      }
    }

    const fc = { type: 'FeatureCollection', features };

    if (map.getSource('targets')) {
      map.getSource('targets').setData(fc);
      raiseMarkerLayers();
      return;
    }

    map.addSource('targets', { type: 'geojson', data: fc });
    map.addLayer({
      id: 'targets-circle',
      type: 'circle',
      source: 'targets',
      minzoom: 10,
      paint: {
        'circle-radius': 5,
        'circle-color': '#cc3333',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#111111',
        'circle-opacity': 0.95,
      },
    });
    map.addLayer({
      id: 'targets-circle-hover',
      type: 'circle',
      source: 'targets',
      minzoom: 10,
      paint: {
        'circle-radius': 7,
        'circle-color': '#cc3333',
        'circle-stroke-width': 2,
        'circle-stroke-color': '#ffffff',
        'circle-opacity': 1,
      },
      filter: ['==', ['get', 'targetKey'], ''],
    });
    map.addLayer({
      id: 'targets-icon',
      type: 'symbol',
      source: 'targets',
      minzoom: 10,
      layout: {
        'icon-image': 'market-target-icon',
        'icon-size': 0.4,
        'icon-allow-overlap': true,
        'icon-ignore-placement': true,
      },
    });
    raiseMarkerLayers();
  }

  const _cityPopup = new maplibregl.Popup({ closeButton: false, offset: 8 });

  function setCityHover(cityId) {
    if (map.getLayer('cities-fill-hover'))
      map.setFilter('cities-fill-hover', ['==', ['get', 'cityId'], cityId || '']);
    if (map.getLayer('cities-beacon-hover'))
      map.setFilter('cities-beacon-hover', ['==', ['get', 'cityId'], cityId || '']);
  }

  map.on('mousemove', 'cities-fill', e => {
    if (!e.features.length) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    map.getCanvas().style.cursor = 'pointer';
    const feature = e.features[0];
    setCityHover(feature.properties.cityId);
    _cityPopup.setLngLat(e.lngLat).setText(feature.properties.name).addTo(map);
  });

  map.on('mouseleave', 'cities-fill', () => {
    map.getCanvas().style.cursor = '';
    setCityHover('');
    _cityPopup.remove();
  });

  map.on('click', 'cities-fill', e => {
    if (!e.features.length) return;
    if (map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    const { cityId } = e.features[0].properties;
    window.MarketDrawer.openCity(cityId);
  });

  map.on('mousemove', 'cities-beacon', e => {
    if (!e.features.length) return;
    if (map.getLayer('targets-circle') && map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    const feature = e.features[0];
    map.getCanvas().style.cursor = 'pointer';
    setCityHover(feature.properties.cityId);
    _cityPopup.setLngLat(feature.geometry.coordinates).setText(feature.properties.name).addTo(map);
  });

  map.on('mouseleave', 'cities-beacon', () => {
    map.getCanvas().style.cursor = '';
    setCityHover('');
    _cityPopup.remove();
  });

  map.on('click', 'cities-beacon', e => {
    if (!e.features.length) return;
    if (map.getLayer('targets-circle') && map.queryRenderedFeatures(e.point, { layers: ['targets-circle'] }).length) return;
    const feature = e.features[0];
    const { cityId } = feature.properties;
    window.MarketDrawer.openCity(cityId);
  });

  map.on('mousemove', 'targets-circle', e => {
    if (!e.features.length) return;
    map.getCanvas().style.cursor = 'pointer';
    const feature = e.features[0];
    map.setFilter('targets-circle-hover', ['==', ['get', 'targetKey'], feature.properties.targetKey]);
    setCityHover('');
    _cityPopup.setLngLat(e.lngLat).setText(`${feature.properties.cityName} — ${feature.properties.targetLabel}`).addTo(map);
  });

  map.on('mouseleave', 'targets-circle', () => {
    map.getCanvas().style.cursor = '';
    map.setFilter('targets-circle-hover', ['==', ['get', 'targetKey'], '']);
    _cityPopup.remove();
  });

  map.on('click', 'targets-circle', e => {
    if (!e.features.length) return;
    const { targetKey } = e.features[0].properties;
    const entry = _targetEntries[targetKey];
    if (entry) window.MarketDrawer.openCity(entry.cityId);
  });

  function locateMarketCity(cityId, entry) {
    const coordinates = entry.marker?.coordinates;
    if (!coordinates) return;
    map.flyTo({
      center: coordinates,
      zoom: Math.max(map.getZoom(), 10),
      duration: 900,
      essential: true,
    });
    setCityHover(cityId);
    setTimeout(() => setCityHover(''), 1800);
  }

  buildTileSwitcher();
  buildGeoTimeline();

  function retryGeolocations() {
    const timer = setInterval(async () => {
      const data = await API.fetchGeolocations();
      if (!Array.isArray(data?.events)) return;
      clearInterval(timer);
      applyGeolocationData(data);
    }, 10_000);
  }

  function retryFortifications() {
    const timer = setInterval(async () => {
      const data = await API.fetchFortifications();
      if (!data?.layers?.length) return;
      clearInterval(timer);
      _fortData = data;
      if (map.isStyleLoaded()) addFortLayers(data);
      renderLegend();
    }, 10_000);
  }

  function initializeCoreLayers() {
    const cityTask = startupCityPromise.then(cityMap => {
      if (!cityMap) return;
      _cityMap = cityMap;
      setMarketCityBasemapLabels(cityMap);
      addCityLayers(cityMap);
      addTargetLayers(cityMap);
      window.MarketDrawer.init(cityMap, {
        onLocate: locateMarketCity,
        onResize: () => map.resize(),
        onGeoLocate: locateGeoEvent,
        onMapChangeLocate: showChangeComparison,
      });
      updateMapChangesContext();
      startupMarketPromise.then(data => window.MarketDrawer.setMarketData(data));
      renderLegend();
    });

    const geoTask = startupGeoPromise.then(data => {
      if (Array.isArray(data?.events)) applyGeolocationData(data);
      else retryGeolocations();
    });

    const mapperTask = startupMapperPromise.then(({ state, index, selected, overlay }) => {
      if (!index?.mappers?.length || !selected) {
        renderMapperMeta(null);
        return;
      }
      if (state?.vector_tiles_enabled) {
        _temporalMode = true;
        _mapState = state;
        _geoDates = [...new Set(state.available_dates || [])].sort();
        _geoDate = state.date;
      }
      _mappers = index.mappers;
      updateMapChangesContext(state);
      _activeMapperId = selected.id;
      buildMapperSwitcher();
      if (!overlay || overlay.error) {
        renderMapperMeta({ display_name: selected.display_name, status: 'error' });
        return;
      }
      _overlayData = overlay;
      renderMapperMeta(overlay);
      addOverlayLayers(overlay);
      renderLegend();
    });

    // Fetching began in parallel above, but heavy fortification layers are only
    // attached after the core mapper, geolocation, and city work has settled.
    Promise.allSettled([mapperTask, geoTask, cityTask]).then(() => {
      // Give the browser a paint with markers and mapper layers before mounting
      // the heavier fortification geometry. Its request has already run in parallel.
      requestAnimationFrame(() => requestAnimationFrame(() => startupFortPromise.then(fortData => {
        if (!fortData?.layers?.length) {
          if (_temporalMode) {
            _fortData = fortData;
            addFortLayers(fortData);
            renderLegend();
            return;
          }
          retryFortifications();
          return;
        }
        _fortData = fortData;
        addFortLayers(fortData);
        renderLegend();
      })));
    });
  }

  function initializeAtStyleData() {
    if (_startupInitialized || !map.getStyle()?.layers?.length) return;
    _startupInitialized = true;
    ensureMapIcons()
      .catch(error => console.warn('Unable to preload local map icons', error))
      .finally(initializeCoreLayers);
  }

  // styledata fires as soon as the style graph exists, before raster/vector tile
  // completion. Core application layers must not wait for MapLibre's load event.
  map.on('styledata', initializeAtStyleData);
  initializeAtStyleData();

  hybridStylePromise.then(style => {
    if (_activeTilesetId === 'hybrid') installBasemap(style);
  }).catch(error => {
    console.warn('OFM style fetch failed, falling back to satellite', error);
    if (_activeTilesetId !== 'hybrid') return;
    _activeTilesetId = 'satellite';
    installBasemap(makeRasterStyle('sat', [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`], 'Tiles © Esri'));
  });

  map.on('style.load', async () => {
    await ensureMapIcons();
    if (!_startupInitialized) initializeAtStyleData();
    else readdDataLayers();
  });

  setInterval(async () => {
    const data = await API.fetchMarketData();
    if (data) window.MarketDrawer.setMarketData(data);
  }, 60_000);

  setInterval(async () => {
    if (!_activeMapperId || !map.isStyleLoaded()) return;
    if (_changeComparison) return;
    if (_temporalMode) {
      const selectedBefore = _geoDate;
      const wasLatest = selectedBefore && selectedBefore === _geoDates.at(-1);
      const state = await API.fetchMapState(wasLatest ? null : selectedBefore);
      if (_geoDate !== selectedBefore || !state?.vector_tiles_enabled) return;
      const dateChanged = state.date !== selectedBefore;
      if (dateChanged) {
        const data = await API.fetchGeolocations(state.date);
        if (_geoDate !== selectedBefore || !Array.isArray(data?.events)) return;
        applyTemporalMapState(state);
        applyGeolocationData(data);
      } else {
        applyTemporalMapState(state);
      }
      return;
    }
    const data = await API.fetchMapperOverlay(_activeMapperId);
    if (!data || data.error) return;
    _overlayData = data;
    renderMapperMeta(data);
    addOverlayLayers(data);
    renderLegend();
  }, 30_000);

  setInterval(async () => {
    if (!map.isStyleLoaded()) return;
    const wasLatest = _geoDate && _geoDate === _geoDates.at(-1);
    const data = await API.fetchGeolocations(wasLatest ? null : _geoDate);
    if (!Array.isArray(data?.events)) return;
    applyGeolocationData(data);
  }, 30 * 60_000); // 30 min — geolocations are daily data

  setInterval(async () => {
    if (_temporalMode) return;
    const mapperIndex = await API.fetchMappers();
    if (!mapperIndex?.mappers?.length) return;
    _mappers = mapperIndex.mappers;
    buildMapperSwitcher();
    if (!_overlayData && _activeMapperId) {
      renderMapperMeta(_mappers.find(mapper => mapper.id === _activeMapperId));
    }
  }, 60_000);

  function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r},${g},${b},${alpha})`;
  }

  function legendSwatchHTML(item) {
    if (item.swatch === 'symbol')
      return legendSymbolHTML(item.color, item.icon, item.borderColor);
    if (item.swatch === 'dashed')
      return `<span class="legend-swatch-dashed" style="border-color:${item.color}"></span>`;
    if (item.swatch === 'circle')
      return `<span class="legend-swatch-circle" style="background:${item.color}"></span>`;
    if (item.swatch === 'line')
      return `<span class="legend-swatch-line" style="background:${item.color}"></span>`;
    // polygon
    const fill = hexToRgba(item.color, item.fillOpacity ?? 0.35);
    return `<span class="legend-swatch-polygon" style="background:${fill};border:2px solid ${item.color}"></span>`;
  }

  function legendSymbolHTML(color, icon, borderColor = '#111111') {
    return `<span class="legend-swatch-symbol" style="background:${color};border-color:${borderColor}"><img src="${icon}" alt=""></span>`;
  }

  function mapperLayerSwatch(layer) {
    const p = layer.paint || {};
    if (layer.geom_type === 'polygon') {
      const color = p.line_color || p.fill_color || '#999';
      const fill = p.fill_color ? hexToRgba(p.fill_color, p.fill_opacity ?? 0.35) : 'transparent';
      return `<span class="legend-swatch-polygon" style="background:${fill};border:2px solid ${color}"></span>`;
    }
    if (layer.geom_type === 'line')
      return `<span class="legend-swatch-line" style="background:${p.line_color || '#999'}"></span>`;
    return `<span class="legend-swatch-circle" style="background:${p.circle_color || '#999'}"></span>`;
  }

  function renderLegend() {
    const el = document.getElementById('legend');
    if (!el) return;

    let html = `<div class="legend-header">Legend</div>`;

    html += `<div class="legend-section"><div class="legend-title">Cities</div>`;
    for (const item of CITY_LEGEND_ITEMS)
      html += `<div class="legend-item">${legendSwatchHTML(item)}${escHtml(item.label)}</div>`;
    html += `</div>`;

    if (_geoData?.events?.length) {
      const sources = (_geoData.sources || []).map(source => source.display_name).join(', ') || 'Geolocations';
      const factions = [...new Map(_geoData.events.map(event => [event.faction_id, event])).values()];
      html += `<div class="legend-section"><div class="legend-title">${escHtml(sources)}</div>`;
      for (const faction of factions) {
        html += `<div class="legend-item">${legendSymbolHTML(faction.faction_color || '#666666', faction.icon_url || MAP_ICONS['geolocation-icon'])}${escHtml(faction.faction_name || 'Unknown')}</div>`;
      }
      html += `</div>`;
    }

    if (_fortData?.layers?.length) {
      html += `<div class="legend-section"><div class="legend-title">Fortifications</div>`;
      for (const layer of _fortData.layers) {
        const checked = _fortLayerVisibility.get(layer.id) !== false;
        html += `<label class="legend-item legend-toggle ${checked ? '' : 'disabled'}">
          <input type="checkbox" data-fort-layer="${escHtml(layer.id)}" ${checked ? 'checked' : ''}>
          ${mapperLayerSwatch(layer)}<span>${escHtml(layer.label)}</span>
        </label>`;
      }
      html += `</div>`;
    }

    if (_overlayData?.layers?.length) {
      const visibleLayers = _overlayData.layers.filter(l => l.geom_type !== 'point');
      if (visibleLayers.length) {
        html += `<div class="legend-section"><div class="legend-title">${escHtml(_overlayData.display_name || 'Overlay')}</div>`;
        for (const layer of visibleLayers)
          html += `<div class="legend-item">${mapperLayerSwatch(layer)}${escHtml(layer.label)}</div>`;
        html += `</div>`;
      }
    }

    el.innerHTML = html;
    el.querySelectorAll('[data-fort-layer]').forEach(input => {
      input.addEventListener('change', () => {
        setFortLayerVisibility(input.dataset.fortLayer, input.checked);
        input.closest('.legend-toggle').classList.toggle('disabled', !input.checked);
      });
    });
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
})();
