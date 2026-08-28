window.MarketDrawer = (() => {
  const TYPE_LABELS = { enter: 'Enter', capture: 'Capture', capture_all: 'Capture all' };
  const TYPE_PRIORITY = ['capture_all', 'capture', 'enter'];

  let _cities = [];
  let _cityEntries = new Map();
  const _activeTypes = new Map();
  let _drawer = null;
  let _marketData = new Map();
  let _resolvedMarkets = [];
  let _marketDataStatus = 'loading';
  let _activeTool = 'markets';
  const _activeViews = new Map();
  const _activeDeadlines = new Map();
  let _onLocate = null;
  let _onResize = null;
  let _onGeoLocate = null;
  let _geoData = { events: [], filters: {} };
  let _selectedGeo = null;
  let _geoDetail = new Map();
  let _focusedCity = null;
  let _showNoResolutions = false;

  function init(cityMap, options = {}) {
    const drawer = document.getElementById('market-drawer');
    if (!drawer) return;

    _drawer = drawer;
    _onLocate = options.onLocate || null;
    _onResize = options.onResize || null;
    _onGeoLocate = options.onGeoLocate || null;
    _cityEntries = new Map();
    _cities = buildCities(cityMap);

    drawer.innerHTML = `
      <div class="market-drawer-header">
        <div class="market-drawer-heading">
          <span class="market-drawer-title">Market cities</span>
          <span class="market-drawer-count">${_cities.length}</span>
        </div>
        <button class="market-drawer-toggle" type="button" aria-label="Collapse market drawer" aria-expanded="true">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"></path></svg>
        </button>
      </div>
      <div class="market-drawer-body">
        <div class="market-drawer-toolbar" role="tablist" aria-label="Map activity tools">
          <button class="market-drawer-tool active" type="button" role="tab" aria-selected="true"
                  data-drawer-tool="markets" aria-label="Active markets" title="Active markets">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V5M4 19h16M7 15l4-4 3 2 5-7"></path></svg>
            <span>Markets</span>
          </button>
          <button class="market-drawer-tool" type="button" role="tab" aria-selected="false"
                  data-drawer-tool="recent" aria-label="Recent resolutions" title="Recent resolutions">
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"></circle><path d="M12 7v5l3 2M8 3H4v4"></path></svg>
            <span>Recent</span>
          </button>
          <button class="market-drawer-tool" type="button" role="tab" aria-selected="false"
                  data-drawer-tool="geolocations" aria-label="Geolocations" title="Geolocations">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s6-5.2 6-12a6 6 0 1 0-12 0c0 6.8 6 12 6 12Z"></path><circle cx="12" cy="9" r="2"></circle></svg>
            <span>Geolocations</span>
          </button>
        </div>
        <label class="market-drawer-search">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"></circle><path d="m16 16 4 4"></path></svg>
          <input type="search" placeholder="Search cities or markets" autocomplete="off" aria-label="Search cities or markets">
        </label>
        <div class="market-drawer-results-meta"></div>
        <div class="market-drawer-list"></div>
      </div>`;

    const input = drawer.querySelector('input[type="search"]');
    const toggle = drawer.querySelector('.market-drawer-toggle');
    input.addEventListener('input', () => renderList(drawer, input.value));
    drawer.querySelectorAll('[data-drawer-tool]').forEach(button => {
      button.addEventListener('click', () => {
        _activeTool = button.dataset.drawerTool;
        input.value = '';
        renderList(drawer, '');
      });
    });
    toggle.addEventListener('click', () => {
      const collapsed = drawer.classList.toggle('collapsed');
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.setAttribute('aria-label', collapsed ? 'Expand market drawer' : 'Collapse market drawer');
      if (_onResize) {
        requestAnimationFrame(_onResize);
        setTimeout(_onResize, 220);
      }
    });
    renderList(drawer, '');
  }

  function buildCities(cityMap) {
    const cities = [];
    for (const [cityId, entry] of Object.entries(cityMap.cities || {})) {
      _cityEntries.set(cityId, entry);
      const markets = [];
      for (const [type, typeMarkets] of Object.entries(entry.markets || {})) {
        for (const market of typeMarkets || []) {
          if (isActiveMarket(market)) markets.push({ ...market, type });
        }
      }
      if (!markets.length) continue;

      const name = (entry.city || {}).name_en || 'Unknown';
      const modalities = TYPE_PRIORITY
        .map(type => {
          const typeMarkets = markets.filter(market => market.type === type);
          return {
            type,
            label: TYPE_LABELS[type],
            markets: typeMarkets,
            groups: groupMarkets(typeMarkets),
            search: normalize(typeMarkets.map(market =>
              `${market.title || ''} ${market.slug || ''} ${market.target?.label || ''}`
            ).join(' ')),
          };
        })
        .filter(modality => modality.markets.length);
      cities.push({
        cityId,
        name,
        modalities,
        search: normalize(`${name} ${markets.map(market => `${market.title || ''} ${market.slug || ''}`).join(' ')}`),
      });
    }
    return cities.sort((a, b) => a.name.localeCompare(b.name));
  }

  function isActiveMarket(market) {
    return market.status ? market.status === 'active' : market.active !== false;
  }

  function groupMarkets(markets) {
    const groups = new Map();
    for (const market of markets) {
      const title = market.title || market.slug || 'Untitled market';
      const label = market.target?.label || questionStem(title);
      const key = market.eventSlug || normalize(label);
      if (!groups.has(key)) groups.set(key, { key, label, markets: [], types: new Set(), search: '' });
      const group = groups.get(key);
      group.markets.push(market);
      group.types.add(market.type);
      group.search += ` ${title} ${market.slug || ''} ${market.target?.label || ''}`;
    }
    return [...groups.values()]
      .map(group => ({
        ...group,
        type: TYPE_PRIORITY.find(type => group.types.has(type)) || 'enter',
        search: normalize(group.search),
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  function renderList(drawer, rawQuery) {
    const recent = _activeTool === 'recent';
    const geolocations = _activeTool === 'geolocations';
    drawer.querySelector('.market-drawer-title').textContent = geolocations ? 'Geolocations' : recent ? 'Recent resolutions' : 'Market cities';
    drawer.querySelector('.market-drawer-count').textContent = geolocations ? (_geoData.events || []).length : recent ? _resolvedMarkets.filter(market => _showNoResolutions || normalize(market.outcome) !== 'no').length : _cities.length;
    drawer.querySelector('.market-drawer-results-meta').classList.toggle('with-resolution-toggle', recent);
    const input = drawer.querySelector('input[type="search"]');
    drawer.querySelector('.market-drawer-search').hidden = geolocations;
    input.placeholder = recent ? 'Search recent resolutions' : 'Search cities or markets';
    input.setAttribute('aria-label', input.placeholder);
    drawer.querySelectorAll('[data-drawer-tool]').forEach(button => {
      const active = button.dataset.drawerTool === _activeTool;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
    });
    if (geolocations) {
      renderGeoList(drawer, rawQuery);
    } else if (recent) {
      renderResolvedList(drawer, rawQuery);
    } else {
      renderActiveList(drawer, rawQuery);
    }
  }

  function renderActiveList(drawer, rawQuery) {
    const query = normalize(rawQuery);
    const matches = _cities
      .filter(city => !query || city.search.includes(query))
      .map(city => {
        const cityMatches = !query || normalize(city.name).includes(query);
        return {
          ...city,
          visibleModalities: city.modalities
            .filter(modality => cityMatches || modality.search.includes(query))
            .map(modality => ({
              ...modality,
              visibleGroups: cityMatches
                ? modality.groups
                : modality.groups.filter(group => group.search.includes(query)),
            })),
        };
      });

    drawer.querySelector('.market-drawer-results-meta').textContent = query
      ? `${matches.length} ${matches.length === 1 ? 'city' : 'cities'} found`
      : `${_cities.length} cities with active markets`;

    const list = drawer.querySelector('.market-drawer-list');
    if (!matches.length) {
      list.innerHTML = '<div class="market-drawer-empty">No matching city or market.</div>';
      return;
    }

    const scrollTop = list.scrollTop;
    list.innerHTML = matches.map(city => {
      const selectedType = _activeTypes.get(city.cityId);
      const activeModality = city.visibleModalities.find(modality => modality.type === selectedType)
        || city.visibleModalities[0];
      if (!activeModality) return '';
      return `
        <section class="market-drawer-city ${city.cityId === _focusedCity ? 'focused' : ''}" data-market-city-item="${escHtml(city.cityId)}">
          <div class="market-drawer-city-header">
            <div class="market-drawer-city-copy">
              <div class="market-drawer-city-name">${escHtml(city.name)}</div>
            </div>
            <button class="market-drawer-locate" type="button" data-city-id="${escHtml(city.cityId)}" aria-label="Find ${escHtml(city.name)} on map" title="Find on map">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6"></circle><path d="m15 15 5 5"></path><path d="M10.5 7.5v6M7.5 10.5h6"></path></svg>
            </button>
          </div>
          <div class="market-drawer-tabs" role="tablist" aria-label="${escHtml(`${city.name} market types`)}">
            ${city.visibleModalities.map(modality => `
              <button class="market-drawer-tab market-tab-${modality.type} ${modality.type === activeModality.type ? 'active' : ''}"
                      type="button" role="tab" aria-selected="${modality.type === activeModality.type}"
                      data-market-city="${escHtml(city.cityId)}" data-market-type="${modality.type}">
                ${escHtml(modality.label)} <span>${modality.markets.length}</span>
              </button>
            `).join('')}
          </div>
          <div class="market-drawer-tab-panel" role="tabpanel">
            ${activeModality.visibleGroups.map(group => marketCardHTML(city, activeModality, group)).join('')}
          </div>
        </section>`;
    }).join('');
    list.scrollTop = scrollTop;

    list.querySelectorAll('[data-city-id]').forEach(button => {
      button.addEventListener('click', () => {
        const cityId = button.dataset.cityId;
        const entry = _cityEntries.get(cityId);
        if (_onLocate && entry) _onLocate(cityId, entry);
      });
    });
    list.querySelectorAll('[data-market-type]').forEach(button => {
      button.addEventListener('click', () => {
        _activeTypes.set(button.dataset.marketCity, button.dataset.marketType);
        renderList(drawer, drawer.querySelector('input[type="search"]').value);
      });
    });
    list.querySelectorAll('[data-market-series]').forEach(wireMarketChart);
    list.querySelectorAll('[data-orderbook-scroll]').forEach(centerOrderbook);
    list.querySelectorAll('[data-market-deadline]').forEach(button => {
      button.addEventListener('click', () => {
        const viewKey = button.dataset.marketDeadline;
        const marketId = button.dataset.marketId;
        if (_activeDeadlines.get(viewKey) === marketId) {
          _activeDeadlines.delete(viewKey);
        } else {
          _activeDeadlines.set(viewKey, marketId);
        }
        renderList(drawer, drawer.querySelector('input[type="search"]').value);
      });
    });
    list.querySelectorAll('[data-market-view]').forEach(button => {
      button.addEventListener('click', () => {
        _activeViews.set(button.dataset.marketViewKey, button.dataset.marketView);
        renderList(drawer, drawer.querySelector('input[type="search"]').value);
      });
    });
  }

  function renderResolvedList(drawer, rawQuery) {
    const query = normalize(rawQuery);
    const searched = _resolvedMarkets.filter(market => !query || market.search.includes(query));
    const matches = searched.filter(market => _showNoResolutions || normalize(market.outcome) !== 'no');
    const groups = groupResolvedMarkets(matches);
    const summary = query
      ? `${groups.length} ${groups.length === 1 ? 'event' : 'events'} found`
      : `${groups.length} resolved ${groups.length === 1 ? 'event' : 'events'}`;
    const meta = drawer.querySelector('.market-drawer-results-meta');
    meta.innerHTML = `<span>${escHtml(summary)}</span><label class="market-resolution-toggle">
      <input type="checkbox" ${_showNoResolutions ? 'checked' : ''}>
      <span class="market-resolution-toggle-track" aria-hidden="true"></span><span>Show No</span></label>`;
    meta.querySelector('input').addEventListener('change', event => {
      _showNoResolutions = event.target.checked;
      renderList(drawer, drawer.querySelector('input[type="search"]').value);
    });

    const list = drawer.querySelector('.market-drawer-list');
    if (!groups.length) {
      list.innerHTML = `<div class="market-drawer-empty">${_marketDataStatus === 'loading'
        ? 'Loading resolved markets...'
        : 'No resolved markets found.'}</div>`;
      return;
    }

    const scrollTop = list.scrollTop;
    let previousDay = '';
    list.innerHTML = groups.map(group => {
      const day = resolvedDayKey(group.resolved_at);
      const divider = day !== previousDay
        ? `<div class="market-resolved-day">${escHtml(formatResolvedDay(group.resolved_at))}</div>`
        : '';
      previousDay = day;
      const viewKey = `recent:${group.key}`;
      const selectedMarketId = _activeDeadlines.get(viewKey);
      const selectedMarket = group.markets.find(market => String(market.id) === selectedMarketId);
      return `${divider}
        <article class="market-resolved-event market-chart-${escHtml(group.type)}">
          <div class="market-resolved-event-header">
            <div>
              <div class="market-resolved-event-title">${escHtml(group.label)}</div>
              <div class="market-resolved-event-context">
                <span>${escHtml(group.city_name || 'Unknown city')}</span>
                <span>${escHtml(TYPE_LABELS[group.type] || group.type || 'Market')}</span>
              </div>
            </div>
            <button class="market-drawer-locate" type="button" data-city-id="${escHtml(group.city_id)}"
                    aria-label="Find ${escHtml(group.city_name || 'city')} on map" title="Find on map">
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6"></circle><path d="m15 15 5 5"></path><path d="M10.5 7.5v6M7.5 10.5h6"></path></svg>
            </button>
          </div>
          ${selectedMarket ? marketVisualizationHTML(selectedMarket, group.label, 'chart') : ''}
          ${resolvedDeadlineRows(group, viewKey, selectedMarketId)}
        </article>`;
    }).join('');
    list.scrollTop = scrollTop;
    list.querySelectorAll('[data-city-id]').forEach(button => {
      button.addEventListener('click', () => {
        const cityId = button.dataset.cityId;
        const entry = _cityEntries.get(cityId);
        if (_onLocate && entry) _onLocate(cityId, entry);
      });
    });
    list.querySelectorAll('[data-market-series]').forEach(wireMarketChart);
    list.querySelectorAll('[data-market-deadline]').forEach(button => {
      button.addEventListener('click', () => {
        const viewKey = button.dataset.marketDeadline;
        const marketId = button.dataset.marketId;
        if (_activeDeadlines.get(viewKey) === marketId) {
          _activeDeadlines.delete(viewKey);
        } else {
          _activeDeadlines.set(viewKey, marketId);
        }
        renderList(drawer, drawer.querySelector('input[type="search"]').value);
      });
    });
  }

  function groupResolvedMarkets(markets) {
    const groups = new Map();
    for (const market of markets) {
      const eventKey = market.event_slug || normalize(questionStem(market.title || market.slug || 'Untitled market'));
      const key = `${market.city_id}:${market.type}:${eventKey}`;
      if (!groups.has(key)) {
        groups.set(key, {
          key,
          label: questionStem(market.title || market.slug || 'Untitled market'),
          city_id: market.city_id,
          city_name: market.city_name,
          type: market.type,
          markets: [],
          resolved_at: market.resolved_at,
        });
      }
      const group = groups.get(key);
      group.markets.push(market);
      if (resolvedTimestamp(market.resolved_at) > resolvedTimestamp(group.resolved_at)) {
        group.resolved_at = market.resolved_at;
      }
    }
    return [...groups.values()]
      .map(group => ({
        ...group,
        markets: group.markets.sort((a, b) => deadlineTimestamp(a) - deadlineTimestamp(b)),
      }))
      .sort((a, b) => resolvedTimestamp(b.resolved_at) - resolvedTimestamp(a.resolved_at));
  }

  function resolvedDeadlineRows(group, viewKey, selectedMarketId) {
    return `
      <div class="market-mock-deadlines market-resolved-deadlines">
        <div class="market-mock-deadline-head">
          <span>Deadline</span><span>Outcome</span><span>Resolved</span>
        </div>
        ${group.markets.map(market => {
          const marketId = String(market.id);
          const selected = marketId === selectedMarketId;
          const outcome = String(market.outcome || 'Invalid');
          const outcomeClass = normalize(outcome) === 'yes' ? 'yes' : normalize(outcome) === 'no' ? 'no' : 'invalid';
          return `
            <button class="market-mock-deadline-row${selected ? ' active' : ''}" type="button"
                    data-market-deadline="${escHtml(viewKey)}" data-market-id="${escHtml(marketId)}"
                    aria-expanded="${selected}">
              <span class="market-mock-deadline-date">${escHtml(formatMarketDeadline(market))}</span>
              <span class="market-resolved-deadline-outcome outcome-${outcomeClass}">${escHtml(outcome)}</span>
              <span class="market-resolved-at">${escHtml(formatResolvedShort(market.resolved_at))}</span>
            </button>`;
        }).join('')}
      </div>`;
  }

  function setMarketData(payload) {
    _marketData = new Map(Object.entries(payload?.markets || {}));
    _resolvedMarkets = (payload?.resolved_markets || [])
      .map(market => ({
        ...market,
        search: normalize(`${market.title || ''} ${market.city_name || ''} ${TYPE_LABELS[market.type] || market.type || ''} ${market.outcome || ''}`),
      }));
    _marketDataStatus = payload?.status || 'error';
    if (!_drawer) return;
    const input = _drawer.querySelector('input[type="search"]');
    renderList(_drawer, input?.value || '');
  }

  function marketCardHTML(city, modality, group) {
    const viewKey = `${city.cityId}:${modality.type}:${group.key}`;
    const selectedMarketId = _activeDeadlines.get(viewKey);
    const selectedMarket = group.markets.find(market => String(market.id) === selectedMarketId);
    const activeView = _activeViews.get(viewKey) || 'chart';
    const visualization = selectedMarket
      ? marketVisualizationHTML(selectedMarket, group.label, activeView)
      : '';
    return `
      <div class="market-mock-card market-chart-${modality.type}">
        <div class="market-mock-header">
          <div class="market-mock-title">${escHtml(group.label)}</div>
          ${selectedMarket ? `<div class="market-view-switch" role="group" aria-label="Market view">
            <button class="market-view-button ${activeView === 'chart' ? 'active' : ''}" type="button"
                    data-market-view="chart" data-market-view-key="${escHtml(viewKey)}" aria-label="Chart view" title="Chart">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 19V5M4 19h16M7 15l4-4 3 2 5-7"></path></svg>
            </button>
            <button class="market-view-button ${activeView === 'orderbook' ? 'active' : ''}" type="button"
                    data-market-view="orderbook" data-market-view-key="${escHtml(viewKey)}" aria-label="Orderbook view" title="Orderbook">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6h14M5 12h14M5 18h14"></path><path d="M8 4v4M15 10v4M11 16v4"></path></svg>
            </button>
          </div>` : ''}
        </div>
        ${visualization}
        ${deadlineRows(group, viewKey, selectedMarketId)}
      </div>`;
  }

  function marketVisualizationHTML(market, label, activeView) {
    const chart = chartMarket(market);
    if (activeView === 'orderbook') return orderbookHTML(chart.live?.orderbook);
    const series = chart.series;
    const points = chartPoints(series);
    const line = points.map(([x, y], pointIndex) => `${pointIndex ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join(' ');
    return `
      <div class="market-mock-chart" data-market-series="${series.join(',')}" data-market-times="${chart.timestamps.join(',')}"
           role="img" aria-label="Polymarket probability history for ${escHtml(label)}, ${escHtml(formatMarketDeadline(market))}">
        <svg viewBox="0 0 280 84" preserveAspectRatio="none" aria-hidden="true">
          <path class="market-mock-grid" d="M6 10H258M6 23.6H258M6 37.2H258M6 50.8H258M6 64.4H258M6 78H258"></path>
          <path class="market-mock-line" d="${line}"></path>
          <line class="market-mock-guide" x1="0" y1="6" x2="0" y2="78"></line>
          <circle class="market-mock-dot" r="3" cx="0" cy="0"></circle>
        </svg>
        <div class="market-mock-y-axis" aria-hidden="true">
          <span>100%</span><span>80%</span><span>60%</span><span>40%</span><span>20%</span><span>0%</span>
        </div>
        <span class="market-mock-tooltip"></span>
      </div>`;
  }

  function orderbookHTML(orderbook) {
    const bids = orderbook?.bids || [];
    const asks = orderbook?.asks || [];
    if (!bids.length && !asks.length) {
      const message = _marketDataStatus === 'loading' ? 'Loading orderbook...' : 'No open orders';
      return `<div class="market-orderbook-empty">${message}</div>`;
    }
    const bidDepth = depthLevels(bids);
    const askDepth = depthLevels(asks).reverse();
    const bestBid = bids[0]?.price;
    const bestAsk = asks[0]?.price;
    const spread = Number.isFinite(Number(bestAsk)) && Number.isFinite(Number(bestBid))
      ? Number(bestAsk) - Number(bestBid)
      : null;
    return `
      <div class="market-orderbook" data-orderbook-scroll role="table" aria-label="Yes token orderbook">
        <div class="market-orderbook-row market-orderbook-head" role="row">
          <span>Price</span><span>Shares</span><span>Total</span>
        </div>
        <div class="market-orderbook-side market-orderbook-asks">
          ${askDepth.map(level => orderbookRowHTML(level, 'ask')).join('')}
        </div>
        <div class="market-orderbook-spread" data-orderbook-spread role="row">
          <span>Last ${formatBookPrice(orderbook.last_trade_price)}</span>
          <span>${spread === null ? '--' : `${Math.round(spread * 100)}&cent;`} spread</span>
        </div>
        <div class="market-orderbook-side market-orderbook-bids">
          ${bidDepth.map(level => orderbookRowHTML(level, 'bid')).join('')}
        </div>
      </div>`;
  }

  function depthLevels(levels) {
    let total = 0;
    const withTotals = levels.map(level => {
      total += Number(level.price) * Number(level.size);
      return { ...level, total };
    });
    const maxTotal = total || 1;
    return withTotals.map(level => ({ ...level, depth: level.total / maxTotal }));
  }

  function orderbookRowHTML(level, side) {
    return `<div class="market-orderbook-row market-book-${side}" role="row" style="--book-depth:${(level.depth * 100).toFixed(2)}%">
      <strong>${formatBookPrice(level.price)}</strong>
      <span>${formatSize(level.size)}</span>
      <span>${formatUsd(level.total)}</span>
    </div>`;
  }

  function formatBookPrice(value) {
    if (value === null || value === undefined) return '--';
    return `${Math.round(Number(value) * 100)}&cent;`;
  }

  function formatSize(value) {
    if (value === null || value === undefined) return '--';
    const size = Number(value);
    if (!Number.isFinite(size)) return '--';
    if (size >= 1000) return `${(size / 1000).toFixed(size >= 10_000 ? 0 : 1)}k`;
    return size.toFixed(size >= 100 ? 0 : 1);
  }

  function formatUsd(value) {
    const amount = Number(value);
    if (!Number.isFinite(amount)) return '--';
    if (amount >= 1_000_000) return `$${(amount / 1_000_000).toFixed(1)}m`;
    if (amount >= 1000) return `$${(amount / 1000).toFixed(amount >= 10_000 ? 0 : 1)}k`;
    return `$${amount.toFixed(amount >= 100 ? 0 : 2)}`;
  }

  function centerOrderbook(orderbook) {
    requestAnimationFrame(() => {
      const spread = orderbook.querySelector('[data-orderbook-spread]');
      if (!spread) return;
      orderbook.scrollTop = spread.offsetTop - (orderbook.clientHeight - spread.offsetHeight) / 2;
    });
  }

  function deadlineRows(group, viewKey, selectedMarketId) {
    const markets = [...group.markets].sort((a, b) => deadlineTimestamp(a) - deadlineTimestamp(b));
    return `
      <div class="market-mock-deadlines">
        <div class="market-mock-deadline-head">
          <span>Deadline</span><span>Yes</span><span>No</span>
        </div>
        ${markets.map(market => {
          const live = _marketData.get(String(market.id));
          const marketId = String(market.id);
          const selected = marketId === selectedMarketId;
          return `
            <button class="market-mock-deadline-row${selected ? ' active' : ''}" type="button"
                    data-market-deadline="${escHtml(viewKey)}" data-market-id="${escHtml(marketId)}"
                    aria-expanded="${selected}">
              <span class="market-mock-deadline-date">${escHtml(formatMarketDeadline(market))}</span>
              <span class="market-mock-yes">${formatOdds(live?.yes)}</span>
              <span class="market-mock-no">${formatOdds(live?.no)}</span>
            </button>`;
        }).join('')}
      </div>`;
  }

  function chartMarket(market) {
    const live = _marketData.get(String(market.id));
    const history = live?.history || [];
    const series = history.map(point => Number(point.p) * 100).filter(Number.isFinite);
    const timestamps = history.map(point => Number(point.t)).filter(Number.isFinite);
    if (!series.length) {
      const hasCurrentOdds = live?.yes !== null && live?.yes !== undefined && Number.isFinite(Number(live.yes));
      const current = hasCurrentOdds ? Number(live.yes) * 100 : 50;
      series.push(current, current);
      const now = Math.floor(Date.now() / 1000);
      timestamps.push(now - 86400, now);
    } else if (series.length === 1) {
      series.unshift(series[0]);
      timestamps.unshift(timestamps[0] - 3600);
    }
    return { market, live, series, timestamps };
  }

  function formatOdds(value) {
    if (value === null || value === undefined) return '--';
    const odds = Number(value);
    return Number.isFinite(odds) ? `${Math.round(odds * 100)}&cent;` : '--';
  }

  function deadlineTimestamp(market) {
    const timestamp = Date.parse(market.deadline || '');
    return Number.isNaN(timestamp) ? Number.POSITIVE_INFINITY : timestamp;
  }

  function formatDeadline(raw) {
    const timestamp = Date.parse(raw || '');
    if (Number.isNaN(timestamp)) return 'No deadline';
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    });
  }

  // Polymarket's close timestamp is often the next UTC day (for example,
  // Sep 1 03:59Z represents a Sep 30 23:59 ET close). The question title is
  // the user-facing calendar deadline, so prefer its explicit "by ..." date.
  function formatMarketDeadline(market) {
    const title = String(market?.title || '');
    const match = title.match(/\s+by\s+(.+?)\?\s*$/i);
    return match ? match[1].trim() : formatDeadline(market?.deadline);
  }

  function resolvedTimestamp(raw) {
    const timestamp = Date.parse(raw || '');
    return Number.isNaN(timestamp) ? 0 : timestamp;
  }

  function formatResolvedShort(raw) {
    const timestamp = Date.parse(raw || '');
    if (Number.isNaN(timestamp)) return 'Unknown';
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'short',
      day: 'numeric',
      timeZone: 'UTC',
    });
  }

  function resolvedDayKey(raw) {
    const timestamp = Date.parse(raw || '');
    return Number.isNaN(timestamp) ? 'unknown' : new Date(timestamp).toISOString().slice(0, 10);
  }

  function formatResolvedDay(raw) {
    const timestamp = Date.parse(raw || '');
    if (Number.isNaN(timestamp)) return 'Unknown date';
    return new Date(timestamp).toLocaleDateString('en-US', {
      month: 'long',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    });
  }

  function formatResolvedTime(raw) {
    const timestamp = Date.parse(raw || '');
    if (Number.isNaN(timestamp)) return 'Time unknown';
    return new Date(timestamp).toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      timeZone: 'UTC',
    });
  }

  function chartPoints(series) {
    return series.map((value, index) => [
      6 + index * (252 / (series.length - 1)),
      78 - value * 0.68,
    ]);
  }

  function wireMarketChart(chart) {
    const series = chart.dataset.marketSeries.split(',').map(Number);
    const timestamps = chart.dataset.marketTimes.split(',').map(Number);
    const points = chartPoints(series);
    const guide = chart.querySelector('.market-mock-guide');
    const dot = chart.querySelector('.market-mock-dot');
    const tooltip = chart.querySelector('.market-mock-tooltip');
    chart.addEventListener('pointermove', event => {
      const rect = chart.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / (rect.width * (258 / 280))));
      const index = Math.round(ratio * (series.length - 1));
      const [x, y] = points[index];
      guide.setAttribute('x1', x);
      guide.setAttribute('x2', x);
      dot.setAttribute('cx', x);
      dot.setAttribute('cy', y);
      const date = new Date(timestamps[index] * 1000).toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        timeZone: 'UTC',
      });
      tooltip.textContent = `${date} · ${Math.round(series[index])}\u00a2`;
      tooltip.style.left = `${(x / 280) * 100}%`;
      chart.classList.add('active');
    });
    chart.addEventListener('pointerleave', () => chart.classList.remove('active'));
  }

  function questionStem(title) {
    return title.replace(/\s+by\s+.+\?$/i, '?').trim();
  }

  function normalize(value) {
    return String(value || '').trim().toLocaleLowerCase();
  }

  function escHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function selectGeo(uuid) {
    _selectedGeo = uuid;
    if (!_geoDetail.has(uuid)) {
      const detail = await API.fetchGeolocation(uuid);
      if (detail) _geoDetail.set(uuid, detail);
    }
    if (_drawer) renderList(_drawer, _drawer.querySelector('input[type="search"]').value);
  }

  function setGeolocations(payload) {
    _geoData = payload || { events: [], filters: {} };
    if (_drawer && _activeTool === 'geolocations') renderList(_drawer, _drawer.querySelector('input[type="search"]').value);
  }

  function openGeolocation(uuid) {
    _activeTool = 'geolocations';
    if (_drawer) {
      _drawer.classList.remove('collapsed');
      _drawer.querySelector('input[type="search"]').value = '';
    }
    selectGeo(uuid).then(() => {
      const item = _drawer?.querySelector(`[data-geo-item="${CSS.escape(uuid)}"]`);
      if (item) item.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  }

  function openCity(cityId) {
    if (!_drawer || !_cities.some(city => city.cityId === cityId)) return;
    _activeTool = 'markets';
    _focusedCity = cityId;
    _drawer.classList.remove('collapsed');
    const toggle = _drawer.querySelector('.market-drawer-toggle');
    toggle?.setAttribute('aria-expanded', 'true');
    toggle?.setAttribute('aria-label', 'Collapse market drawer');
    const input = _drawer.querySelector('input[type="search"]');
    input.value = '';
    renderList(_drawer, '');
    requestAnimationFrame(() => {
      _drawer.querySelector(`[data-market-city-item="${CSS.escape(cityId)}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'auto' });
      if (_onResize) _onResize();
    });
  }

  function renderGeoList(drawer) {
    const events = _geoData.events || [];
    drawer.querySelector('.market-drawer-results-meta').textContent = `${events.length} geolocated ${events.length === 1 ? 'event' : 'events'}`;
    const list = drawer.querySelector('.market-drawer-list');
    if (!events.length) { list.innerHTML = '<div class="market-drawer-empty">No matching geolocations.</div>'; return; }
    list.innerHTML = events.map(event => {
      const expanded = event.uuid === _selectedGeo;
      const detail = _geoDetail.get(event.uuid);
      const reported = event.time_precision === 'minute' ? new Date(event.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : 'Time not specified';
      return `<article class="geo-drawer-item ${expanded ? 'expanded' : ''}" data-geo-item="${escHtml(event.uuid)}">
        <div class="geo-drawer-head"><button class="geo-drawer-main" type="button" data-geo-select="${escHtml(event.uuid)}" aria-expanded="${expanded}">
          <span class="geo-drawer-badge" style="--geo-color:${escHtml(event.faction_color || '#666')}">${escHtml(event.faction_name || 'Unknown')}</span>
          <span class="geo-drawer-time">${escHtml(reported)}</span><span class="geo-drawer-desc">${escHtml(event.description)}</span></button>
          <button class="market-drawer-locate" type="button" data-geo-locate="${escHtml(event.uuid)}" title="Find on map" aria-label="Find event on map"><svg viewBox="0 0 24 24"><circle cx="10.5" cy="10.5" r="6"></circle><path d="m15 15 5 5"></path><path d="M10.5 7.5v6M7.5 10.5h6"></path></svg></button></div>
        ${expanded ? geoDetailHTML(detail || event) : ''}</article>`;
    }).join('');
    list.querySelectorAll('[data-geo-select]').forEach(button => button.addEventListener('click', () => selectGeo(button.dataset.geoSelect)));
    list.querySelectorAll('[data-geo-locate]').forEach(button => button.addEventListener('click', () => {
      const event = (_geoData.events || []).find(item => item.uuid === button.dataset.geoLocate);
      if (event && _onGeoLocate) _onGeoLocate(event);
    }));
    wireGeoEmbeds(list);
  }

  function geoDetailHTML(event) {
    const evidence = event.evidence_links || [];
    const first = evidence.find(url => socialEmbed(url));
    return `<div class="geo-drawer-detail">${first ? socialEmbed(first) : ''}${evidence.filter(url => url !== first).length ? `<div class="geo-link-section"><strong>Evidence</strong>${evidence.filter(url => url !== first).map(sourceLink).join('')}</div>` : ''}
      ${(event.geolocation_links || []).length ? `<div class="geo-link-section"><strong>Geolocation proof</strong>${event.geolocation_links.map(sourceLink).join('')}</div>` : ''}
      <a class="geo-attribution" href="https://geoconfirmed.org/ukraine" target="_blank" rel="noopener">Verified by GeoConfirmed ↗</a></div>`;
  }

  function sourceLink(url) { return `<a href="${escHtml(url)}" target="_blank" rel="noopener">Open source ↗</a>`; }
  function socialEmbed(url) {
    try {
      const parsed = new URL(url), host = parsed.hostname.replace(/^www\./, '').toLowerCase();
      const tweet = ['x.com', 'twitter.com', 'mobile.twitter.com'].includes(host) && parsed.pathname.match(/\/status\/(\d+)/);
      if (tweet) return `<div class="geo-social" data-geo-tweet="${tweet[1]}" data-url="${escHtml(url)}">Loading X post…</div>`;
      const parts = parsed.pathname.split('/').filter(Boolean); if (parts[0] === 's') parts.shift();
      if (['t.me', 'telegram.me'].includes(host) && parts.length === 2 && parts[0] !== 'c' && /^\d+$/.test(parts[1])) return `<div class="geo-social" data-geo-telegram="${escHtml(parts.join('/'))}" data-url="${escHtml(url)}">Loading Telegram post…</div>`;
    } catch (_error) {}
    return '';
  }

  function wireGeoEmbeds(root) {
    root.querySelectorAll('[data-geo-tweet]').forEach(node => {
      node.innerHTML = `<blockquote class="twitter-tweet" data-theme="dark"><a href="${escHtml(node.dataset.url)}"></a></blockquote>`;
      const script = document.createElement('script'); script.src = 'https://platform.twitter.com/widgets.js'; script.async = true; node.appendChild(script);
    });
    root.querySelectorAll('[data-geo-telegram]').forEach(node => {
      const script = document.createElement('script'); script.src = 'https://telegram.org/js/telegram-widget.js?22'; script.async = true; script.dataset.telegramPost = node.dataset.geoTelegram; script.dataset.width = '100%'; script.dataset.dark = '1'; node.replaceChildren(script);
    });
  }

  return { init, setMarketData, setGeolocations, openGeolocation, openCity };
})();
