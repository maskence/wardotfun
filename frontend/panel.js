window.Panel = (() => {
  const TABS = ['enter', 'capture', 'capture_all'];
  const TAB_LABELS = { enter: 'Enter', capture: 'Capture', capture_all: 'Capture All' };

  let _popup   = null;
  let _markets = null;
  let _activeTab = null;

  // ── Public ────────────────────────────────────────────────────────────────

  function open(cityId, cityEntry, lngLat, point, map) {
    _markets   = cityEntry.markets || {};
    _activeTab = TABS.find(t => activeMarkets(t).length > 0) || 'enter';

    if (_popup) { _popup.remove(); _popup = null; }

    const anchor   = point.y < map.getContainer().clientHeight / 3 ? 'top' : 'bottom';
    const cityName = (cityEntry.city || {}).name_en || 'Unknown';

    _popup = new maplibregl.Popup({ className: 'city-popup', closeButton: true, maxWidth: '340px', anchor, offset: 12 })
      .setLngLat(lngLat)
      .setHTML(buildHTML(cityName))
      .addTo(map);

    const el = _popup.getElement();
    renderMarkets(el);
    wireTabs(el);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function activeMarkets(tab) {
    return (_markets[tab] || []).filter(m => m.active !== false);
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  function buildHTML(cityName) {
    const visibleTabs = TABS.filter(t => activeMarkets(t).length > 0);
    const tabsHTML = visibleTabs.length > 1
      ? `<div class="popup-tabs">${visibleTabs.map(t =>
          `<button class="popup-tab${t === _activeTab ? ' active' : ''}" data-tab="${t}">${TAB_LABELS[t]}</button>`
        ).join('')}</div>`
      : '';
    return `<div class="popup-inner"><div class="popup-city-name">${escHtml(cityName)}</div>${tabsHTML}<div class="popup-markets"></div></div>`;
  }

  function renderMarkets(el) {
    const list = activeMarkets(_activeTab);
    const body = el.querySelector('.popup-markets');
    if (!list.length) {
      body.innerHTML = '<div class="no-markets">No active markets for this city.</div>';
      return;
    }
    body.innerHTML = groupMarkets(list).map(groupHTML).join('');
  }

  function wireTabs(el) {
    el.querySelectorAll('.popup-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeTab = btn.dataset.tab;
        el.querySelectorAll('.popup-tab').forEach(b => b.classList.toggle('active', b === btn));
        renderMarkets(el);
      });
    });
  }

  function groupMarkets(markets) {
    const groups = new Map();

    for (const market of markets) {
      const title = marketTitle(market);
      const key = marketGroupKey(market);
      if (!groups.has(key)) {
        groups.set(key, { label: groupLabel(title), sortKey: groupSortKey(title), markets: [] });
      }
      groups.get(key).markets.push(market);
    }

    return [...groups.values()]
      .sort((a, b) => a.sortKey.localeCompare(b.sortKey) || a.label.localeCompare(b.label))
      .map(group => ({
        ...group,
        markets: group.markets.sort((a, b) => marketRowSortKey(a) - marketRowSortKey(b) || marketRowLabel(a).localeCompare(marketRowLabel(b))),
      }));
  }

  function groupHTML(group) {
    return `
      <section class="market-group">
        <div class="market-group-title">${escHtml(group.label)}</div>
        ${group.markets.map(cardHTML).join('')}
      </section>`;
  }

  function cardHTML(market) {
    const title    = marketRowLabel(market);
    const polyUrl  = market.eventSlug && market.slug
      ? `https://polymarket.com/event/${market.eventSlug}/${market.slug}`
      : null;
    const titleEl  = polyUrl
      ? `<a class="market-title market-title-link" href="${escHtml(polyUrl)}" target="_blank" rel="noopener">${escHtml(title)}</a>`
      : `<div class="market-title">${escHtml(title)}</div>`;
    return `
      <div class="market-card">
        ${titleEl}
      </div>`;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function marketTitle(market) {
    return market.title || market.slug || '—';
  }

  function marketGroupKey(market) {
    const stem = questionStem(marketTitle(market));
    return stem.toLowerCase();
  }

  function groupLabel(title) {
    return questionStem(title);
  }

  function groupSortKey(title) {
    return questionStem(title).toLowerCase();
  }

  function marketRowLabel(market) {
    return extractQuestionDate(marketTitle(market)) || formatDeadline(market.deadline);
  }

  function marketRowSortKey(market) {
    const extracted = extractQuestionDate(marketTitle(market));
    if (extracted) {
      const ts = Date.parse(extracted);
      if (!Number.isNaN(ts)) return ts;
    }
    const raw = market.deadline;
    if (!raw) return Number.POSITIVE_INFINITY;
    const ts = Date.parse(raw);
    return Number.isNaN(ts) ? Number.POSITIVE_INFINITY : ts;
  }

  function questionStem(title) {
    return title.replace(/\s+by\s+.+\?$/i, '?').trim();
  }

  function extractQuestionDate(title) {
    const match = title.match(/\s+by\s+(.+)\?$/i);
    return match ? match[1].trim() : null;
  }

  function formatDeadline(raw) {
    if (!raw) return 'No close date';
    try {
      const d = new Date(raw);
      return isNaN(d) ? raw : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch { return raw; }
  }

  function escHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return { open };
})();
