window.API = (() => {
  async function request(path) {
    const resp = await fetch(path);
    if (!resp.ok) return null;
    return resp.json();
  }

  // Begin core localhost requests before the external MapLibre script and
  // basemap styles load. Regular fetch methods below remain uncached for polling.
  const startup = {
    mapState: request('/api/map-state'),
    mappers: request('/api/mappers'),
    cityMap: request('/api/city-market-map'),
    marketData: request('/api/market-data'),
    geolocations: request('/api/geolocations'),
  };

  return {
    startup,
    fetchMapState: (date = null) => request(`/api/map-state${date ? `?date=${encodeURIComponent(date)}` : ''}`),
    fetchMapChanges: ({ date = null, source = null, cursor = null, limit = 20 } = {}) => {
      const params = new URLSearchParams();
      if (date) params.set('date', date);
      if (source) params.set('source', source);
      if (cursor) params.set('cursor', cursor);
      params.set('limit', limit);
      return request(`/api/map-changes?${params}`);
    },
    fetchMapChangeStatus: (date = null, after = null) => {
      const params = new URLSearchParams();
      if (date) params.set('date', date);
      if (after) params.set('after', after);
      const query = params.toString();
      return request(`/api/map-changes/status${query ? `?${query}` : ''}`);
    },
    fetchMapChange: areaId => request(`/api/map-changes/v2/${encodeURIComponent(areaId)}`),
    fetchMappers: () => request('/api/mappers'),
    fetchMapperOverlay: (mapperId) => request(`/api/mapper-overlay?mapper=${encodeURIComponent(mapperId)}`),
    fetchFortifications: () => request('/api/fortifications'),
    fetchCityMarketMap: () => request('/api/city-market-map'),
    fetchMarketData: () => request('/api/market-data'),
    fetchGeolocations: (date = null, filters = {}) => {
      const params = new URLSearchParams();
      if (date) params.set('date', date);
      for (const key of ['q', 'faction', 'icon', 'origin']) {
        if (filters[key]) params.set(key, filters[key]);
      }
      const query = params.toString();
      return request(`/api/geolocations${query ? `?${query}` : ''}`);
    },
    fetchGeolocation: uuid => request(`/api/geolocations/${encodeURIComponent(uuid)}`),
  };
})();
