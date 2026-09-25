(() => {
  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services';
  const OFM_STYLE_URL = 'https://tiles.openfreemap.org/styles/liberty';
  const areaId = new URLSearchParams(window.location.search).get('area');

  function absoluteTileUrl(template) {
    return new URL(template, window.location.href).href.replace(/%7B(z|x|y)%7D/gi, '{$1}');
  }

  async function hybridStyle() {
    const response = await fetch(OFM_STYLE_URL);
    if (!response.ok) throw new Error(`basemap style failed: ${response.status}`);
    const style = await response.json();
    style.layers = style.layers
      .filter(layer => layer.type === 'line' || layer.type === 'symbol')
      .map(layer => ({
        ...layer,
        paint: layer.type === 'symbol' ? {
          ...layer.paint,
          'text-color': '#ffffff',
          'text-halo-color': '#000000',
          'text-halo-width': 1.5,
        } : layer.paint,
      }));
    style.sources.satellite = {
      type: 'raster',
      tiles: [`${ESRI}/World_Imagery/MapServer/tile/{z}/{y}/{x}`],
      tileSize: 256,
      attribution: 'Tiles © Esri',
    };
    style.layers.unshift({ id: 'satellite-base', type: 'raster', source: 'satellite' });
    return style;
  }

  function metadata(layer) {
    return layer.source_layer ? {
      'source-layer': layer.source_layer,
      filter: ['==', ['get', 'layer_key'], layer.id],
    } : {};
  }

  function addSnapshotLayer(map, sourceId, layer) {
    const paint = layer.paint || {};
    const vector = metadata(layer);
    const prefix = `snapshot-${layer.id}`;
    if (layer.geom_type === 'polygon') {
      map.addLayer({ id: `${prefix}-fill`, type: 'fill', source: sourceId, ...vector, paint: {
        'fill-color': ['coalesce', ['get', 'fill_color'], paint.fill_color || '#999999'],
        'fill-opacity': ['coalesce', ['get', 'fill_opacity'], paint.fill_opacity ?? 0.25],
      }});
      map.addLayer({ id: `${prefix}-line`, type: 'line', source: sourceId, ...vector, paint: {
        'line-color': ['coalesce', ['get', 'line_color'], paint.line_color || paint.fill_color || '#999999'],
        'line-opacity': ['coalesce', ['get', 'line_opacity'], paint.line_opacity ?? 1],
        'line-width': ['coalesce', ['get', 'line_width'], paint.line_width ?? 1.5],
      }});
    } else if (layer.geom_type === 'line') {
      map.addLayer({ id: `${prefix}-line`, type: 'line', source: sourceId, ...vector, paint: {
        'line-color': ['coalesce', ['get', 'line_color'], paint.line_color || '#999999'],
        'line-opacity': ['coalesce', ['get', 'line_opacity'], paint.line_opacity ?? 1],
        'line-width': ['coalesce', ['get', 'line_width'], paint.line_width ?? 1.5],
      }});
    } else {
      map.addLayer({ id: `${prefix}-circle`, type: 'circle', source: sourceId, ...vector, paint: {
        'circle-color': ['coalesce', ['get', 'circle_color'], paint.circle_color || '#999999'],
        'circle-opacity': ['coalesce', ['get', 'circle_opacity'], paint.circle_opacity ?? 1],
        'circle-radius': ['coalesce', ['get', 'circle_radius'], paint.circle_radius ?? 4],
        'circle-stroke-color': paint.circle_stroke_color || '#ffffff',
        'circle-stroke-width': paint.circle_stroke_width ?? 1.5,
      }});
    }
  }

  function hatchId(color) {
    return `removed-hatch-${String(color).replace(/[^0-9a-f]/gi, '').toLowerCase() || '999999'}`;
  }

  function addHatch(map, color) {
    const id = hatchId(color);
    if (map.hasImage(id)) return id;
    let hex = String(color || '#999999').replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(value => value + value).join('');
    if (!/^[0-9a-f]{6}([0-9a-f]{2})?$/i.test(hex)) hex = '999999';
    const rgb = [0, 2, 4].map(index => parseInt(hex.slice(index, index + 2), 16));
    const size = 32;
    const data = new Uint8Array(size * size * 4);
    for (let y = 0; y < size; y += 1) for (let x = 0; x < size; x += 1) {
      const offset = (y * size + x) * 4;
      const stripe = ((x + y) % 16) < 3;
      data.set([rgb[0], rgb[1], rgb[2], stripe ? 235 : 0], offset);
    }
    map.addImage(id, { width: size, height: size, data }, { pixelRatio: 2 });
    return id;
  }

  function addChanges(map, detail) {
    const sourceId = `changes-${detail.id}`;
    map.addSource(sourceId, { type: 'vector', tiles: [absoluteTileUrl(detail.change_tile_url)], minzoom: 0, maxzoom: 14 });
    const colors = [...new Set([...(detail.removed_fill_colors || []), '#999999'])];
    const pairs = [];
    for (const color of colors) pairs.push(color, ['image', addHatch(map, color)]);
    const removedPattern = ['match', ['coalesce', ['get', 'fill_color'], '#999999'], ...pairs, ['image', addHatch(map, '#999999')]];
    const variants = [
      { key: 'added-after', type: 'added', phase: 'after', indicator: '#54e383', dash: [0.6, 1.5], restore: false },
      { key: 'removed-before', type: 'removed', phase: 'before', indicator: '#ff6868', dash: [3, 2], restore: true },
      { key: 'modified-before', type: 'modified', phase: 'before', indicator: '#ff6868', dash: [3, 2], restore: true },
      { key: 'modified-after', type: 'modified', phase: 'after', indicator: '#54e383', dash: [0.6, 1.5], restore: false },
      { key: 'modified-style', type: 'modified', phase: 'style', indicator: '#f0c75e', dash: [2, 2], restore: false },
    ];
    for (const variant of variants) {
      const filter = [['==', ['get', 'change_type'], variant.type], ['==', ['get', 'phase'], variant.phase]];
      if (variant.restore) {
        map.addLayer({ id: `${variant.key}-fill`, type: 'fill', source: sourceId, 'source-layer': 'changes',
          filter: ['all', ...filter, ['==', ['geometry-type'], 'Polygon']],
          paint: { 'fill-pattern': removedPattern, 'fill-opacity': 1 } });
      }
      map.addLayer({ id: `${variant.key}-line`, type: 'line', source: sourceId, 'source-layer': 'changes',
        filter: ['all', ...filter, ['!=', ['geometry-type'], 'Point']],
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': variant.indicator, 'line-width': 3, 'line-opacity': 1, 'line-dasharray': variant.dash } });
      map.addLayer({ id: `${variant.key}-point`, type: 'circle', source: sourceId, 'source-layer': 'changes',
        filter: ['all', ...filter, ['==', ['geometry-type'], 'Point']],
        paint: { 'circle-color': variant.restore ? ['coalesce', ['get', 'circle_color'], ['get', 'fill_color'], '#999999'] : 'rgba(0,0,0,0)',
          'circle-opacity': variant.restore ? 0.4 : 1, 'circle-radius': ['+', ['coalesce', ['get', 'circle_radius'], 4], 2],
          'circle-stroke-color': variant.indicator, 'circle-stroke-width': 3 } });
    }
  }

  async function render() {
    if (!areaId) throw new Error('missing map-change area');
    const [detailResponse, style] = await Promise.all([
      fetch(`/api/map-changes/v2/${encodeURIComponent(areaId)}`),
      hybridStyle(),
    ]);
    if (!detailResponse.ok) throw new Error(`map change failed: ${detailResponse.status}`);
    const detail = await detailResponse.json();
    const [west, south, east, north] = detail.bounds;
    const map = new maplibregl.Map({
      container: 'thumbnail-map', style, interactive: false, attributionControl: true,
      fadeDuration: 0, preserveDrawingBuffer: true,
      bounds: [[west, south], [east, north]],
      fitBoundsOptions: { padding: 38, maxZoom: 12, duration: 0 },
    });
    let sceneInstalled = false;
    const markReady = () => {
      if (document.documentElement.dataset.renderReady === 'true') return;
      document.documentElement.dataset.renderReady = 'true';
      window.__WARDOTFUN_THUMBNAIL_READY__ = true;
    };
    map.on('style.load', () => {
      if (sceneInstalled) return;
      sceneInstalled = true;
      const snapshot = detail.after;
      const snapshotSource = `snapshot-${snapshot.id}`;
      map.addSource(snapshotSource, { type: 'vector', tiles: [absoluteTileUrl(snapshot.tile_url)], minzoom: 0, maxzoom: 14 });
      for (const layer of snapshot.layers || []) addSnapshotLayer(map, snapshotSource, layer);
      const changeSource = `changes-${detail.id}`;
      addChanges(map, detail);
      document.getElementById('thumbnail-caption').textContent = `${detail.source.display_name} · map change`;
      map.on('idle', async () => {
        if (!map.areTilesLoaded() || !map.isSourceLoaded(snapshotSource) || !map.isSourceLoaded(changeSource)) return;
        if (document.fonts?.ready) await document.fonts.ready;
        requestAnimationFrame(() => requestAnimationFrame(markReady));
      });
    });
    map.on('error', event => console.error('thumbnail map error', event.error || event));
  }

  render().catch(error => {
    console.error(error);
    document.getElementById('thumbnail-status').textContent = 'Map thumbnail unavailable';
    document.body.dataset.renderError = 'true';
  });
})();
