window.Panel = (() => {
  const TABS = ['enter', 'capture', 'capture_all'];
  const TAB_LABELS = { enter: 'Enter', capture: 'Capture', capture_all: 'Capture All' };

  let _popup   = null;
  let _markets = null;
  let _activeTab = null;
  let _activeGroupKey = null;
  let _activeMarketSlug = null;

  // ── Public ────────────────────────────────────────────────────────────────

  function open(cityId, cityEntry, lngLat, point, map) {
    _markets   = cityEntry.markets || {};
    _activeTab = TABS.find(t => activeMarkets(t).length > 0) || 'enter';
    resetMarketSelection();

    if (_popup) { _popup.remove(); _popup = null; }

    const anchor   = point.y < map.getContainer().clientHeight / 3 ? 'top' : 'bottom';
    const cityName = (cityEntry.city || {}).name_en || 'Unknown';

    _popup = new maplibregl.Popup({ className: 'city-popup', closeButton: false, maxWidth: '420px', anchor, offset: 12 })
      .setLngLat(lngLat)
      .setHTML(buildHTML(cityName))
      .addTo(map);

    const el = _popup.getElement();
    renderMarkets(el);
    wireTabs(el);
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function isActiveMarket(market) {
    return market.status ? market.status === 'active' : market.active !== false;
  }

  function activeMarkets(tab) {
    return (_markets[tab] || []).filter(isActiveMarket);
  }

  // ── Rendering ─────────────────────────────────────────────────────────────

  function buildHTML(cityName) {
    const visibleTabs = TABS.filter(t => activeMarkets(t).length > 0);
    const tabsHTML = visibleTabs.length > 1
      ? `<div class="popup-tabs">${visibleTabs.map(t =>
          `<button class="popup-tab${t === _activeTab ? ' active' : ''}" data-tab="${t}">${TAB_LABELS[t]}</button>`
        ).join('')}</div>`
      : '';
    return `<div class="popup-inner">${tabsHTML}<div class="popup-markets"></div></div>`;
  }

  function renderMarkets(el) {
    const list = activeMarkets(_activeTab);
    const body = el.querySelector('.popup-markets');
    if (!list.length) {
      body.innerHTML = '<div class="no-markets">No active markets for this city.</div>';
      return;
    }
    const groups = groupMarkets(list);
    syncSelection(groups);
    const activeGroup = groups.find(group => group.key === _activeGroupKey) || groups[0];
    const activeMarket = activeGroup.markets.find(m => marketSlug(m) === _activeMarketSlug) || activeGroup.markets[0];
    body.innerHTML = buildEmbedHTML(groups, activeGroup, activeMarket);
    wireEmbedSelectors(el, groups);
  }

  function wireTabs(el) {
    el.querySelectorAll('.popup-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeTab = btn.dataset.tab;
        resetMarketSelection();
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
        const label = market.target?.label || groupLabel(title);
        groups.set(key, { key, label, sortKey: label.toLowerCase(), markets: [] });
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

  function buildEmbedHTML(groups, activeGroup, activeMarket) {
    const groupTabs = groups.length > 1
      ? `<div class="popup-subtabs-wrap"><div class="popup-subtabs">${groups.map(group =>
          `<button class="popup-subtab${group.key === activeGroup.key ? ' active' : ''}" data-group-key="${escHtml(group.key)}">${escHtml(group.label)}</button>`
        ).join('')}</div>${closeButtonHTML()}</div>`
      : `<div class="market-group-title"><span class="market-group-title-text">${escHtml(activeGroup.label)}</span>${closeButtonHTML()}</div>`;

    const dateTabs = activeGroup.markets.length > 1
      ? `<div class="popup-date-tabs">${activeGroup.markets.map(market =>
          `<button class="popup-date-tab${marketSlug(market) === marketSlug(activeMarket) ? ' active' : ''}" data-market-slug="${escHtml(marketSlug(market))}">${escHtml(marketRowLabel(market))}</button>`
        ).join('')}</div>`
      : '';

    return `
      <section class="market-group market-group-embed">
        ${groupTabs}
        <div class="market-embed-frame-wrap">
          <iframe
            class="market-embed-frame"
            title="${escHtml(`polymarket-${marketSlug(activeMarket)}`)}"
            src="${escHtml(embedUrl(activeMarket))}"
            loading="lazy"
            frameborder="0"
            allowtransparency="true">
          </iframe>
        </div>
        ${dateTabs}
      </section>`;
  }

  function wireEmbedSelectors(el, groups) {
    el.querySelectorAll('[data-close-popup]').forEach(btn => {
      btn.addEventListener('click', () => {
        if (_popup) {
          _popup.remove();
          _popup = null;
        }
      });
    });

    el.querySelectorAll('[data-group-key]').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeGroupKey = btn.dataset.groupKey;
        const group = groups.find(item => item.key === _activeGroupKey);
        _activeMarketSlug = group?.markets[0] ? marketSlug(group.markets[0]) : null;
        renderMarkets(el);
      });
    });

    el.querySelectorAll('[data-market-slug]').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeMarketSlug = btn.dataset.marketSlug;
        renderMarkets(el);
      });
    });
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function resetMarketSelection() {
    _activeGroupKey = null;
    _activeMarketSlug = null;
  }

  function syncSelection(groups) {
    if (!_activeGroupKey || !groups.some(group => group.key === _activeGroupKey)) {
      _activeGroupKey = groups[0]?.key || null;
    }
    const activeGroup = groups.find(group => group.key === _activeGroupKey);
    if (!_activeMarketSlug || !activeGroup?.markets.some(market => marketSlug(market) === _activeMarketSlug)) {
      _activeMarketSlug = activeGroup?.markets[0] ? marketSlug(activeGroup.markets[0]) : null;
    }
  }

  function marketTitle(market) {
    return market.title || market.slug || '—';
  }

  function marketSlug(market) {
    return market.slug || market.id || marketTitle(market);
  }

  function marketGroupKey(market) {
    if (market.eventSlug) return market.eventSlug;
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

  function embedUrl(market) {
    const params = new URLSearchParams({
      market: marketSlug(market),
      theme: 'dark',
      layout: 'standard',
      height: '300',
    });
    return `https://embed.polymarket.com/market?${params.toString()}`;
  }

  function closeButtonHTML() {
    return '<button class="popup-close-tab" type="button" aria-label="Close market popup" data-close-popup>&times;</button>';
  }

  function escHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  return { open };
})();
