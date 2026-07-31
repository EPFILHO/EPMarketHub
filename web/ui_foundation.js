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
    VIEW_METADATA,
    compareTerminal,
    switchView,
  });
}(typeof globalThis !== 'undefined' ? globalThis : this));
