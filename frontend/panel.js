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
    body.innerHTML = list.map(m => cardHTML(m)).join('');
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

  function cardHTML(market) {
    const title    = market.title || market.slug || '—';
    const deadline = market.deadline ? formatDeadline(market.deadline) : null;
    const polyUrl  = market.eventSlug && market.slug
      ? `https://polymarket.com/event/${market.eventSlug}/${market.slug}`
      : null;
    const titleEl  = polyUrl
      ? `<a class="market-title market-title-link" href="${escHtml(polyUrl)}" target="_blank" rel="noopener">${escHtml(title)}</a>`
      : `<div class="market-title">${escHtml(title)}</div>`;
    return `
      <div class="market-card">
        ${titleEl}
        ${deadline ? `<div class="market-deadline">Closes ${escHtml(deadline)}</div>` : ''}
      </div>`;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function formatDeadline(raw) {
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
