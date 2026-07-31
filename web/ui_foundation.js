(function exposeMarketHubUi(root) {
  'use strict';

  const VIEW_METADATA = Object.freeze({
    terminals: Object.freeze([
      'Terminais MT5',
      'Crie instâncias controladas e mantenha múltiplas conexões simultâneas.',
    ]),
    symbols: Object.freeze([
      'Ativos',
      'Ativos lógicos e aliases são resolvidos de forma independente em cada MT5.',
    ]),
    dashboard: Object.freeze([
      'Dashboard',
      'Dados de mercado recebidos das suas fontes MT5.',
    ]),
    diagnostics: Object.freeze([
      'Diagnóstico',
      'Fluxos simultâneos e snapshots para validar as conexões do kernel.',
    ]),
  });
  const ATTENTION_PROCESS_STATES = new Set([
    'launch_failed',
    'close_failed',
    'duplicate_process',
  ]);
  const ATTENTION_WORKER_STATES = new Set([
    'waiting_login',
    'authentication_failed',
    'account_mismatch',
    'broker_disconnected',
    'configuration_error',
    'terminal_mismatch',
    'unresponsive',
    'attention_required',
    'worker_start_failed',
    'worker_crashed',
    'stop_failed',
    'error',
  ]);
  const THEME_STORAGE_KEY = 'ep_market_hub_theme_v1';

  function normalizeTheme(theme) {
    return theme === 'dark' ? 'dark' : 'light';
  }

  function storedTheme(storage) {
    try {
      const value = storage?.getItem(THEME_STORAGE_KEY);
      return value === 'light' || value === 'dark' ? value : null;
    } catch (_) {
      return null;
    }
  }

  function applyTheme(documentRef, theme) {
    const normalized = normalizeTheme(theme);
    if (!documentRef?.documentElement) return normalized;
    documentRef.documentElement.dataset.theme = normalized;
    const button = documentRef.getElementById?.('themeToggle');
    if (button) {
      const nextTheme = normalized === 'light' ? 'dark' : 'light';
      button.setAttribute('aria-pressed', normalized === 'dark' ? 'true' : 'false');
      button.title = `Usar tema ${nextTheme === 'dark' ? 'escuro' : 'claro'}`;
      const icon = button.querySelector?.('[data-theme-icon]');
      const label = button.querySelector?.('[data-theme-label]');
      if (icon) icon.textContent = nextTheme === 'dark' ? '☾' : '☀';
      if (label) label.textContent = nextTheme === 'dark' ? 'Tema escuro' : 'Tema claro';
    }
    return normalized;
  }

  function initializeTheme(documentRef, storage) {
    return applyTheme(documentRef, storedTheme(storage) || 'light');
  }

  function setTheme(documentRef, storage, theme) {
    const normalized = applyTheme(documentRef, theme);
    try {
      storage?.setItem(THEME_STORAGE_KEY, normalized);
    } catch (_) {
      // A aparência continua funcional mesmo sem armazenamento local.
    }
    return normalized;
  }

  function bindThemeToggle(documentRef, storage) {
    const button = documentRef?.getElementById?.('themeToggle');
    const current = initializeTheme(documentRef, storage);
    if (!button || button.dataset.themeBound === '1') return current;
    button.dataset.themeBound = '1';
    button.addEventListener('click', () => {
      const active = normalizeTheme(documentRef.documentElement.dataset.theme);
      setTheme(documentRef, storage, active === 'light' ? 'dark' : 'light');
    });
    return current;
  }

  function compareTerminal(a, b) {
    const labelCompare = String(a?.label || '').localeCompare(
      String(b?.label || ''),
      'pt-BR',
      { sensitivity: 'base', numeric: true },
    );
    if (labelCompare) return labelCompare;
    const brokerCompare = String(a?.broker_name || '').localeCompare(
      String(b?.broker_name || ''),
      'pt-BR',
      { sensitivity: 'base', numeric: true },
    );
    if (brokerCompare) return brokerCompare;
    return String(a?.account_login || '').localeCompare(
      String(b?.account_login || ''),
      'pt-BR',
      { sensitivity: 'base', numeric: true },
    );
  }

  function numberTerminals(rows) {
    return (Array.isArray(rows) ? rows : [])
      .slice()
      .sort(compareTerminal)
      .map((terminal, index) => ({ ...terminal, display_number: index + 1 }));
  }

  function terminalDisplayNumber(terminal) {
    const value = Number(terminal?.display_number);
    return Number.isInteger(value) && value > 0 ? String(value).padStart(2, '0') : '—';
  }

  function terminalNeedsAttention(terminal, worker = {}) {
    const instanceState = terminal?.instance_status?.state || 'ready';
    if (instanceState !== 'ready') return true;
    if (ATTENTION_PROCESS_STATES.has(terminal?.process_state)) return true;
    return ATTENTION_WORKER_STATES.has(worker?.state);
  }

  function terminalHealthSummary(rows, states = {}) {
    const terminals = Array.isArray(rows) ? rows : [];
    const workers = terminals.map(terminal => states[terminal.id] || terminal.worker || {});
    return {
      registered: terminals.length,
      open: terminals.filter(terminal => Boolean(terminal.running)).length,
      workers: workers.filter(worker => Boolean(worker.alive)).length,
      connected: workers.filter(worker => Boolean(worker.connected)).length,
      attention: terminals.filter((terminal, index) => (
        terminalNeedsAttention(terminal, workers[index])
      )).length,
    };
  }

  function marketQuoteRows(snapshotMap = {}, liveTickMap = {}) {
    const quotesBySource = new Map();

    function marketNumber(value) {
      if (value === null || value === undefined || value === '') return null;
      const number = Number(value);
      return Number.isFinite(number) ? number : null;
    }

    function addQuote(tick, terminal = {}, fallbackTimestamp = '') {
      if (!tick || tick.ok === false) return;
      const bid = marketNumber(tick.bid);
      const ask = marketNumber(tick.ask);
      if (bid === null && ask === null) return;

      const terminalId = String(tick.terminal_id || terminal.id || '');
      const logicalId = String(
        tick.logical_id || tick.name || tick.resolved_symbol || tick.symbol || '',
      );
      if (!terminalId || !logicalId) return;

      const receivedAt = String(tick.received_at || fallbackTimestamp || '');
      const receivedScore = Date.parse(receivedAt);
      const quote = {
        terminalId,
        logicalId,
        name: String(tick.name || tick.logical_id || tick.resolved_symbol || tick.symbol || ''),
        symbol: String(tick.resolved_symbol || tick.symbol || ''),
        bid,
        ask,
        spread: marketNumber(tick.spread),
        terminalLabel: String(tick.terminal_label || terminal.label || terminalId),
        brokerName: String(tick.broker_name || terminal.broker_name || ''),
        receivedAt,
        receivedScore: Number.isFinite(receivedScore) ? receivedScore : 0,
      };
      const key = `${terminalId}\u0000${logicalId}`;
      const current = quotesBySource.get(key);
      if (!current || quote.receivedScore >= current.receivedScore) {
        quotesBySource.set(key, quote);
      }
    }

    Object.values(snapshotMap || {}).forEach(snapshot => {
      const terminal = snapshot?.terminal || {};
      (snapshot?.ticks || []).forEach(tick => addQuote(tick, terminal, snapshot?.timestamp));
    });
    Object.values(liveTickMap || {}).forEach(tick => addQuote(tick));

    return Array.from(quotesBySource.values()).sort((a, b) => (
      b.receivedScore - a.receivedScore
      || a.name.localeCompare(b.name, 'pt-BR', { sensitivity: 'base', numeric: true })
      || a.terminalLabel.localeCompare(b.terminalLabel, 'pt-BR', { sensitivity: 'base', numeric: true })
    ));
  }

  function marketQuoteSummary(snapshotMap = {}, liveTickMap = {}) {
    const quotes = marketQuoteRows(snapshotMap, liveTickMap);
    return {
      quotes,
      assets: new Set(quotes.map(quote => quote.logicalId)).size,
      sources: new Set(quotes.map(quote => quote.terminalId)).size,
      lastUpdated: quotes[0]?.receivedAt || '',
    };
  }

  function switchView(documentRef, view) {
    const metadata = VIEW_METADATA[view];
    if (!documentRef || !metadata) return false;
    documentRef.querySelectorAll('.nav-item').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      if (active) button.setAttribute?.('aria-current', 'page');
      else button.removeAttribute?.('aria-current');
    });
    documentRef.querySelectorAll('.view').forEach(element => {
      element.classList.toggle('active', element.id === `view-${view}`);
    });
    const title = documentRef.getElementById('viewTitle');
    const subtitle = documentRef.getElementById('viewSubtitle');
    if (title) title.textContent = metadata[0];
    if (subtitle) subtitle.textContent = metadata[1];
    return true;
  }

  root.MarketHubUI = Object.freeze({
    THEME_STORAGE_KEY,
    VIEW_METADATA,
    applyTheme,
    bindThemeToggle,
    compareTerminal,
    initializeTheme,
    marketQuoteRows,
    marketQuoteSummary,
    normalizeTheme,
    numberTerminals,
    setTheme,
    storedTheme,
    switchView,
    terminalDisplayNumber,
    terminalHealthSummary,
    terminalNeedsAttention,
  });
}(typeof globalThis !== 'undefined' ? globalThis : this));
