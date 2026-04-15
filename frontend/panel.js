window.Panel = (() => {
  const panel = document.getElementById('city-panel');
  const TABS = ['enter', 'capture', 'capture_all'];
  const TAB_LABELS = { enter: 'Enter', capture: 'Capture', capture_all: 'Capture All' };

  let _oddsCache = {}; // conditionId → {yes, no} | null
  let _activeTab = null;
  let _currentCity = null;

  // ── Public ────────────────────────────────────────────────────────────────

  function open(cityId, cityEntry) {
    _currentCity = { cityId, cityEntry };
    const cityName = (cityEntry.city || {}).name_en || 'Unknown';
    const markets = cityEntry.markets || {};

    // Pick first tab that has markets
    const firstTab = TABS.find(t => (markets[t] || []).length > 0) || 'enter';
    _activeTab = firstTab;

    renderPanel(cityName, markets);
    panel.classList.remove('hidden');
  }

  function close() {
    panel.classList.add('hidden');
    _currentCity = null;
    _activeTab = null;
  }

  // ── Render ────────────────────────────────────────────────────────────────

  function renderPanel(cityName, markets) {
    panel.innerHTML = `
      <div class="panel-header">
        <span class="panel-city-name">${escHtml(cityName)}</span>
        <button class="panel-close" id="panel-close-btn">×</button>
      </div>
      ${renderTabs(markets)}
      <div class="panel-body" id="panel-body"></div>
    `;

    document.getElementById('panel-close-btn').addEventListener('click', close);

    panel.querySelectorAll('.tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        _activeTab = btn.dataset.tab;
        panel.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b === btn));
        renderMarketList(markets[_activeTab] || []);
      });
    });

    renderMarketList(markets[_activeTab] || []);
  }

  function renderTabs(markets) {
    const visibleTabs = TABS.filter(t => (markets[t] || []).length > 0);
    if (visibleTabs.length <= 1) return ''; // no tabs needed for single type
    const buttons = visibleTabs.map(t => `
      <button class="tab-btn ${t === _activeTab ? 'active' : ''}" data-tab="${t}">
        ${TAB_LABELS[t]}
      </button>
    `).join('');
    return `<div class="panel-tabs">${buttons}</div>`;
  }

  function renderMarketList(marketList) {
    const body = document.getElementById('panel-body');
    if (!marketList.length) {
      body.innerHTML = '<div class="no-markets">No markets for this city.</div>';
      return;
    }
    body.innerHTML = marketList.map((m, i) => renderMarketCard(m, i)).join('');

    // Fetch odds for each market
    marketList.forEach((m, i) => {
      const conditionId = m.conditionId;
      if (!conditionId) return;
      if (_oddsCache[conditionId] !== undefined) {
        updateOddsEl(i, _oddsCache[conditionId]);
      } else {
        API.fetchOdds(conditionId).then(odds => {
          _oddsCache[conditionId] = odds;
          updateOddsEl(i, odds);
        });
      }
    });
  }

  function renderMarketCard(market, idx) {
    const title = market.title || market.slug || '—';
    const deadline = market.deadline ? formatDeadline(market.deadline) : null;
    const slug = market.slug || '';
    const polyUrl = slug ? `https://polymarket.com/event/${slug}` : null;

    return `
      <div class="market-card">
        <div class="market-title">${escHtml(title)}</div>
        ${deadline ? `<div class="market-deadline">Closes ${escHtml(deadline)}</div>` : ''}
        <div class="market-odds" id="odds-${idx}">
          <span class="odds-loading">loading odds…</span>
        </div>
        ${polyUrl ? `<a class="market-link" href="${escHtml(polyUrl)}" target="_blank" rel="noopener">→ Bet on Polymarket</a>` : ''}
      </div>
    `;
  }

  function updateOddsEl(idx, odds) {
    const el = document.getElementById(`odds-${idx}`);
    if (!el) return;
    if (!odds) {
      el.innerHTML = '<span class="odds-unavailable">odds unavailable</span>';
      return;
    }
    const yesPct = Math.round((odds.yes || 0) * 100);
    const noPct = Math.round((odds.no || 0) * 100);
    el.innerHTML = `
      <span class="odds-pill yes">YES ${yesPct}¢</span>
      <span class="odds-pill no">NO ${noPct}¢</span>
    `;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────

  function formatDeadline(raw) {
    try {
      const d = new Date(raw);
      if (isNaN(d)) return raw;
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch {
      return raw;
    }
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  return { open, close };
})();
