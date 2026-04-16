window.API = (() => {
  async function request(path) {
    const resp = await fetch(path);
    if (!resp.ok) return null;
    return resp.json();
  }

  return {
    fetchISWLayers: () => request('/api/isw-layers'),
    fetchCityMarketMap: () => request('/api/city-market-map'),
  };
})();
