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
      'Fluxos rápidos e snapshots consolidados vindos de workers persistentes.',
    ]),
  });
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

  function switchView(documentRef, view) {
    const metadata = VIEW_METADATA[view];
    if (!documentRef || !metadata) return false;
    documentRef.querySelectorAll('.nav-item').forEach(button => {
      button.classList.toggle('active', button.dataset.view === view);
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
    normalizeTheme,
    setTheme,
    storedTheme,
    switchView,
  });
}(typeof globalThis !== 'undefined' ? globalThis : this));
