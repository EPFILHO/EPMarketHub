let bridge = null;
let terminals = [];
let symbols = [];
let workerStates = {};
let snapshots = {};
let liveStreams = {};
let liveTicks = {};
let runtimeLimits = { max_active_mt5: 3, registered: 0, open_mt5: 0, active_workers: 0 };
let workerSummarySignature = '';
let dashboardTerminalSignature = '';
const SELECTED_TERMINALS_KEY = 'ep_market_hub_selected_terminals_v1';
let selectedTerminalIds = new Set();

const LIVE_SLOT_IDS = ['live-1', 'live-2', 'live-3'];
const LIVE_PREFERENCES = [
  ['eurusd', 'usdjpy', 'bitcoin'],
  ['bitcoin', 'usdjpy', 'eurusd'],
  ['winq26', 'wdo', 'win', 'bitcoin', 'eurusd']
];

function closeEnhancedSelects(except = null) {
  document.querySelectorAll('.select-shell.open').forEach(shell => {
    if (shell === except) return;
    shell.classList.remove('open');
    shell.closest('.live-card, .card')?.classList.remove('select-open-host');
  });
}

function refreshEnhancedSelect(select) {
  if (!select?._enhancedSelect) return;
  const { trigger, label, menu, shell } = select._enhancedSelect;
  const selected = select.options[select.selectedIndex] || select.options[0];
  label.textContent = selected?.textContent || 'Selecione...';
  trigger.disabled = select.disabled;
  menu.innerHTML = Array.from(select.options).map(option => `
    <button type="button" class="select-option ${option.value === select.value ? 'selected' : ''}"
            data-value="${escapeHtml(option.value)}" ${option.disabled ? 'disabled' : ''}>
      ${escapeHtml(option.textContent || '')}
    </button>
  `).join('');
  menu.querySelectorAll('.select-option').forEach(button => {
    button.addEventListener('click', event => {
      event.stopPropagation();
      select.value = button.dataset.value || '';
      select.dispatchEvent(new Event('change', { bubbles: true }));
      refreshEnhancedSelect(select);
      shell.classList.remove('open');
      shell.closest('.live-card, .card')?.classList.remove('select-open-host');
    });
  });
}

function enhanceSelect(select) {
  if (!select || select._enhancedSelect) {
    refreshEnhancedSelect(select);
    return;
  }

  const shell = document.createElement('div');
  shell.className = 'select-shell';
  select.parentNode.insertBefore(shell, select);
  shell.appendChild(select);
  select.classList.add('native-select-source');
  select.tabIndex = -1;

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'select-trigger';
  trigger.innerHTML = '<span class="select-trigger-label"></span><span class="select-chevron">⌄</span>';

  const menu = document.createElement('div');
  menu.className = 'select-menu';

  shell.appendChild(trigger);
  shell.appendChild(menu);
  const label = trigger.querySelector('.select-trigger-label');
  select._enhancedSelect = { shell, trigger, label, menu };

  trigger.addEventListener('click', event => {
    event.stopPropagation();
    const willOpen = !shell.classList.contains('open');
    closeEnhancedSelects(shell);
    shell.classList.toggle('open', willOpen);
    shell.closest('.live-card, .card')?.classList.toggle('select-open-host', willOpen);
    if (willOpen) refreshEnhancedSelect(select);
  });
  select.addEventListener('change', () => refreshEnhancedSelect(select));
  refreshEnhancedSelect(select);
}

function enhanceAllSelects() {
  document.querySelectorAll('select').forEach(enhanceSelect);
}

function parseResponse(text) {
  try { return JSON.parse(text); }
  catch (e) { return { ok: false, message: 'Resposta inválida da ponte Python.', data: text }; }
}

function toast(message, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast ${isError ? 'error' : ''}`;
  setTimeout(() => el.classList.add('hidden'), 5200);
}

function setBridgeStatus(ok, text) {
  const el = document.getElementById('bridgeStatus');
  el.textContent = text;
  el.classList.toggle('ok', ok);
}

function switchView(view) {
  document.querySelectorAll('.nav-item').forEach(btn => btn.classList.toggle('active', btn.dataset.view === view));
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
  const titles = {
    terminals: ['Terminais MT5', 'Crie instâncias controladas e mantenha múltiplas conexões simultâneas.'],
    symbols: ['Ativos', 'Ativos lógicos e aliases são resolvidos de forma independente em cada MT5.'],
    dashboard: ['Dashboard', 'Fluxos rápidos e snapshots consolidados vindos de workers persistentes.']
  };
  document.getElementById('viewTitle').textContent = titles[view][0];
  document.getElementById('viewSubtitle').textContent = titles[view][1];
}


function setTextIfChanged(element, value) {
  if (!element) return;
  const next = String(value ?? '');
  if (element.textContent !== next) element.textContent = next;
}

function setHtmlIfChanged(element, value) {
  if (!element) return;
  const next = String(value ?? '');
  if (element.innerHTML !== next) element.innerHTML = next;
}

function compareTerminal(a, b) {
  const labelCompare = String(a?.label || '').localeCompare(String(b?.label || ''), 'pt-BR', { sensitivity: 'base', numeric: true });
  if (labelCompare) return labelCompare;
  const brokerCompare = String(a?.broker_name || '').localeCompare(String(b?.broker_name || ''), 'pt-BR', { sensitivity: 'base', numeric: true });
  if (brokerCompare) return brokerCompare;
  return String(a?.account_login || '').localeCompare(String(b?.account_login || ''), 'pt-BR', { sensitivity: 'base', numeric: true });
}

function connectedTerminals() {
  return terminals
    .filter(t => Boolean((workerStates[t.id] || t.worker || {}).connected))
    .slice()
    .sort(compareTerminal);
}

function populateSnapshotTerminalSelector() {
  const select = document.getElementById('snapshotTerminal');
  if (!select) return;
  const connected = connectedTerminals();
  const before = select.value;
  select.innerHTML = '';

  if (!connected.length) {
    select.innerHTML = '<option value="">Nenhum terminal conectado</option>';
    refreshEnhancedSelect(select);
    renderSelectedSnapshot();
    return;
  }

  connected.forEach(t => {
    const option = document.createElement('option');
    option.value = t.id;
    option.textContent = `${t.label || t.id}${t.broker_name ? ` · ${t.broker_name}` : ''}`;
    select.appendChild(option);
  });
  select.value = connected.some(t => t.id === before) ? before : connected[0].id;
  refreshEnhancedSelect(select);
  renderSelectedSnapshot();
}

function refreshDashboardTerminalSources(force = false) {
  const signature = connectedTerminals()
    .map(t => `${t.id}:${t.label || ''}:${t.broker_name || ''}`)
    .join('|');
  if (!force && signature === dashboardTerminalSignature) return;
  dashboardTerminalSignature = signature;
  populateSnapshotTerminalSelector();
  populateLiveTerminalSelectors();
}

function restoreSelectedTerminals() {
  try {
    const raw = JSON.parse(localStorage.getItem(SELECTED_TERMINALS_KEY) || '[]');
    if (Array.isArray(raw)) selectedTerminalIds = new Set(raw.map(String));
  } catch (_) {
    selectedTerminalIds = new Set();
  }
}

function persistSelectedTerminals() {
  try {
    localStorage.setItem(SELECTED_TERMINALS_KEY, JSON.stringify(Array.from(selectedTerminalIds)));
  } catch (_) {
    // O app continua funcionando mesmo se o armazenamento do Chromium estiver indisponível.
  }
}

function selectedTerminalList() {
  return terminals.filter(t => selectedTerminalIds.has(t.id)).map(t => t.id);
}

function updateSelectionUi() {
  const maxActive = Number(runtimeLimits.max_active_mt5 || 3);
  const validIds = new Set(terminals.map(t => t.id));
  selectedTerminalIds = new Set(Array.from(selectedTerminalIds).filter(id => validIds.has(id)).slice(0, maxActive));
  persistSelectedTerminals();

  document.querySelectorAll('[data-terminal-select]').forEach(input => {
    const checked = selectedTerminalIds.has(input.dataset.terminalSelect);
    if (input.checked !== checked) input.checked = checked;
    input.closest('.terminal-item')?.classList.toggle('selected', checked);
  });

  const count = selectedTerminalIds.size;
  setTextIfChanged(document.getElementById('selectionStatus'), `Selecionados: ${count} de ${maxActive}`);
  const openButton = document.getElementById('btnOpenSelected');
  const closeButton = document.getElementById('btnCloseSelected');
  if (openButton) openButton.disabled = count === 0;
  if (closeButton) closeButton.disabled = count === 0;
}

function toggleTerminalSelection(terminalId, checked) {
  const maxActive = Number(runtimeLimits.max_active_mt5 || 3);
  if (checked && !selectedTerminalIds.has(terminalId) && selectedTerminalIds.size >= maxActive) {
    const input = document.querySelector(`[data-terminal-select="${terminalId}"]`);
    if (input) input.checked = false;
    toast(`Selecione no máximo ${maxActive} terminais ao mesmo tempo.`, true);
    return;
  }
  if (checked) selectedTerminalIds.add(terminalId);
  else selectedTerminalIds.delete(terminalId);
  updateSelectionUi();
}

function workerLabel(state) {
  const labels = {
    stopped: 'leitura parada',
    starting: 'iniciando',
    connected: 'conectado',
    waiting_login: 'aguardando login',
    reconnecting: 'reconectando',
    error: 'erro'
  };
  return labels[state] || state || 'parado';
}

function workerBadgeClass(worker) {
  if (worker?.connected) return 'ok';
  if (worker?.state === 'waiting_login' || worker?.state === 'reconnecting' || worker?.state === 'starting') return 'warn';
  if (worker?.state === 'error') return 'bad';
  return '';
}

function renderTerminals(rows) {
  terminals = (rows || []).slice().sort(compareTerminal);
  terminals.forEach(t => {
    if (t.worker) workerStates[t.id] = t.worker;
  });

  const list = document.getElementById('terminalList');

  if (!terminals.length) {
    list.className = 'list empty';
    list.textContent = 'Nenhum terminal cadastrado ainda.';
    selectedTerminalIds.clear();
    updateSelectionUi();
    refreshDashboardTerminalSources(true);
    updateWorkersStatus();
    renderWorkerSummary();
    return;
  }

  list.className = 'list';
  const maxActive = Number(runtimeLimits.max_active_mt5 || 3);
  const openCount = terminals.filter(t => t.running).length;
  const activeWorkerCount = terminals.filter(t => (workerStates[t.id] || t.worker || {}).alive).length;
  list.innerHTML = terminals.map(t => {
    const worker = workerStates[t.id] || t.worker || {};
    const openBlocked = !t.running && openCount >= maxActive;
    const waitingConnection = Boolean(worker.alive && !worker.connected);
    const readingBlocked = !t.running
      || waitingConnection
      || (!worker.alive && activeWorkerCount >= maxActive);
    const readingLabel = worker.connected
      ? 'Parar leitura'
      : (waitingConnection ? 'Aguardando conexão' : 'Iniciar leitura');
    const limitTitle = `Limite de ${maxActive} MT5 simultâneos atingido`;
    const readingTitle = !t.running
      ? 'Abra o MT5 para habilitar a leitura'
      : (waitingConnection
          ? 'Aguardando o MT5 confirmar login e conexão com a corretora'
          : ((!worker.alive && activeWorkerCount >= maxActive) ? limitTitle : ''));
    const selected = selectedTerminalIds.has(t.id);
    return `
      <div class="terminal-item ${selected ? 'selected' : ''}" id="terminalItem-${escapeHtml(t.id)}" data-terminal-id="${escapeHtml(t.id)}">
        <div class="terminal-select-row">
          <label class="terminal-select-label">
            <input type="checkbox" data-terminal-select="${escapeHtml(t.id)}" ${selected ? 'checked' : ''}
                   onchange="toggleTerminalSelection('${escapeJs(t.id)}', this.checked)" />
            <span>Selecionar</span>
          </label>
        </div>
        <div class="terminal-title">
          <div>
            <strong>${escapeHtml(t.label || t.id)}</strong>
            <div class="muted">${escapeHtml(t.broker_name || 'Corretora não informada')} · ${escapeHtml(t.account_login || 'login manual')}</div>
          </div>
          <div class="badge-stack">
            <span class="badge mt5-badge ${t.running ? 'ok' : ''}">MT5 ${t.running ? 'aberto' : 'fechado'}</span>
            <span class="badge worker-badge ${workerBadgeClass(worker)}">${escapeHtml(workerLabel(worker.state))}</span>
          </div>
        </div>
        <small class="muted path">${escapeHtml(t.terminal_exe || '')}</small>
        <div class="worker-detail">${terminalWorkerDetailHtml(worker)}</div>
        <div class="actions">
          <button onclick="openEditTerminal('${escapeJs(t.id)}')">Editar</button>
          <button data-role="open-button" onclick="launchTerminal('${escapeJs(t.id)}')"
                  ${t.running || openBlocked ? 'disabled' : ''}
                  title="${t.running ? 'MT5 já está aberto' : (openBlocked ? limitTitle : 'Abre o MT5 e inicia a leitura')}">Abrir MT5</button>
          <button data-role="reading-button" class="${worker.connected ? '' : 'success'}"
                  onclick="toggleReading('${escapeJs(t.id)}')" ${readingBlocked ? 'disabled' : ''}
                  title="${readingTitle}">${readingLabel}</button>
          <button data-role="snapshot-button" onclick="refreshSnapshot('${escapeJs(t.id)}')" ${worker.connected ? '' : 'disabled'}>Snapshot</button>
          <button data-role="reconnect-button" onclick="reconnectWorker('${escapeJs(t.id)}')" ${worker.connected ? '' : 'disabled'}>Reconectar</button>
          <button data-role="close-button" class="danger" onclick="stopTerminal('${escapeJs(t.id)}')" ${t.running ? '' : 'disabled'}>Fechar MT5</button>
          <button data-role="delete-button" class="danger" onclick="openDeleteTerminal('${escapeJs(t.id)}')" ${t.running ? 'disabled' : ''} title="${t.running ? 'Feche o MT5 antes de excluir a instância' : 'Exclui o cadastro e a pasta local da instância'}">Excluir</button>
        </div>
      </div>
    `;
  }).join('');

  updateSelectionUi();
  refreshDashboardTerminalSources(true);
  updateWorkersStatus();
  renderWorkerSummary();
  renderSelectedSnapshot();
  updateLiveProof();
}

function terminalWorkerDetailHtml(worker) {
  return `
    <span>${escapeHtml(worker?.message || 'Leitura parada.')}</span>
    ${worker?.pid ? `<span>PID worker: ${escapeHtml(worker.pid)}</span>` : ''}
    ${worker?.account_login ? `<span>Conta conectada: ${escapeHtml(worker.account_login)} · ${escapeHtml(worker.server || '')}</span>` : ''}
    ${worker?.last_snapshot ? `<span>Último snapshot: ${escapeHtml(worker.last_snapshot)}</span>` : ''}
  `;
}

function updateTerminalWorkerRows() {
  const maxActive = Number(runtimeLimits.max_active_mt5 || 3);
  const activeWorkerCount = terminals.filter(t => (workerStates[t.id] || t.worker || {}).alive).length;
  const openCount = terminals.filter(t => t.running).length;

  terminals.forEach(t => {
    const worker = workerStates[t.id] || t.worker || {};
    const item = document.getElementById(`terminalItem-${t.id}`);
    if (!item) return;

    const workerBadge = item.querySelector('.worker-badge');
    if (workerBadge) {
      workerBadge.className = `badge worker-badge ${workerBadgeClass(worker)}`;
      setTextIfChanged(workerBadge, workerLabel(worker.state));
    }
    setHtmlIfChanged(item.querySelector('.worker-detail'), terminalWorkerDetailHtml(worker));

    const limitTitle = `Limite de ${maxActive} MT5 simultâneos atingido`;
    const openButton = item.querySelector('[data-role="open-button"]');
    if (openButton) {
      const blocked = !t.running && openCount >= maxActive;
      openButton.disabled = Boolean(t.running || blocked);
      openButton.title = t.running
        ? 'MT5 já está aberto'
        : (blocked ? limitTitle : 'Abre o MT5 e inicia a leitura');
    }

    const readingButton = item.querySelector('[data-role="reading-button"]');
    if (readingButton) {
      const waitingConnection = Boolean(worker.alive && !worker.connected);
      const blocked = !t.running
        || waitingConnection
        || (!worker.alive && activeWorkerCount >= maxActive);
      readingButton.disabled = blocked;
      readingButton.classList.toggle('success', !worker.connected);
      setTextIfChanged(
        readingButton,
        worker.connected ? 'Parar leitura' : (waitingConnection ? 'Aguardando conexão' : 'Iniciar leitura')
      );
      readingButton.title = !t.running
        ? 'Abra o MT5 para habilitar a leitura'
        : (waitingConnection
            ? 'Aguardando o MT5 confirmar login e conexão com a corretora'
            : ((!worker.alive && activeWorkerCount >= maxActive) ? limitTitle : ''));
    }

    const snapshotButton = item.querySelector('[data-role="snapshot-button"]');
    if (snapshotButton) snapshotButton.disabled = !worker.connected;
    const reconnectButton = item.querySelector('[data-role="reconnect-button"]');
    if (reconnectButton) reconnectButton.disabled = !worker.connected;
    const closeButton = item.querySelector('[data-role="close-button"]');
    if (closeButton) closeButton.disabled = !t.running;
    const deleteButton = item.querySelector('[data-role="delete-button"]');
    if (deleteButton) {
      deleteButton.disabled = Boolean(t.running);
      deleteButton.title = t.running
        ? 'Feche o MT5 antes de excluir a instância'
        : 'Exclui o cadastro e a pasta local da instância';
    }
  });
}

function applyWorkerStates(rows) {
  (rows || []).forEach(row => { workerStates[row.terminal_id] = row; });
  terminals = terminals.map(t => ({ ...t, worker: workerStates[t.id] || t.worker }));
  updateTerminalWorkerRows();
  updateWorkersStatus();
  refreshDashboardTerminalSources();
  renderWorkerSummary();
  LIVE_SLOT_IDS.forEach((_, index) => renderLiveSlot(index + 1));
  updateLiveProof();
}

function updateWorkersStatus() {
  const all = terminals.map(t => workerStates[t.id] || t.worker || {});
  const alive = all.filter(w => w.alive).length;
  const connected = all.filter(w => w.connected).length;
  const el = document.getElementById('workersStatus');
  const limit = Number(runtimeLimits.max_active_mt5 || 3);
  el.textContent = `Leituras: ${connected} conectadas · ${alive}/${limit} ativas`;
  el.classList.toggle('ok', connected > 0);
}

function renderWorkerSummary() {
  const el = document.getElementById('workerSummary');
  const connected = connectedTerminals();
  const signature = connected.map(t => t.id).join('|');
  if (!connected.length) {
    if (el.innerHTML) el.innerHTML = '';
    workerSummarySignature = '';
    return;
  }
  if (workerSummarySignature !== signature) {
    el.innerHTML = connected.map(t => `
      <article class="summary-card connected" id="summary-${escapeHtml(t.id)}">
        <div class="summary-top">
          <strong>${escapeHtml(t.label || t.id)}</strong>
          <span class="status-dot ok"></span>
        </div>
        <div class="summary-value"></div>
        <small></small>
      </article>
    `).join('');
    workerSummarySignature = signature;
  }
  connected.forEach(t => {
    const worker = workerStates[t.id] || t.worker || {};
    const card = document.getElementById(`summary-${t.id}`);
    if (!card) return;
    const dot = card.querySelector('.status-dot');
    if (dot) dot.className = 'status-dot ok';
    setTextIfChanged(card.querySelector('.summary-value'), 'Conectado');
    setTextIfChanged(card.querySelector('small'), `PID ${worker.pid || '—'} · ${worker.server || worker.message || ''}`);
  });
}

function renderSymbols(rows) {
  symbols = rows || [];
  const el = document.getElementById('symbolList');
  if (!symbols.length) {
    el.innerHTML = '<div class="muted">Nenhum ativo cadastrado.</div>';
    populateLiveSymbolSelectors();
    return;
  }
  el.innerHTML = symbols.map(s => `
    <div class="symbol-card">
      <h4>${escapeHtml(s.name)}</h4>
      <small>${escapeHtml(s.category)} · ${s.enabled ? 'ativo' : 'inativo'}</small>
      <small>Aliases: ${escapeHtml((s.aliases || []).join(', '))}</small>
      <small>Uso: ${escapeHtml((s.role || []).join(', '))}</small>
    </div>
  `).join('');
  populateLiveSymbolSelectors();
}

function populateLiveTerminalSelectors() {
  const connected = connectedTerminals();
  for (let i = 1; i <= 3; i++) {
    const select = document.getElementById(`liveTerminal${i}`);
    if (!select) continue;
    const before = select.value;
    select.innerHTML = '<option value="">Selecione um terminal conectado...</option>';
    connected.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = `${t.label || t.id}${t.broker_name ? ` · ${t.broker_name}` : ''}`;
      select.appendChild(opt);
    });
    if (connected.some(t => t.id === before)) {
      select.value = before;
    } else if (connected[i - 1]) {
      select.value = connected[i - 1].id;
    } else {
      select.value = '';
    }
    refreshEnhancedSelect(select);
  }
}

function populateLiveSymbolSelectors() {
  const enabled = symbols.filter(s => s.enabled);
  for (let i = 1; i <= 3; i++) {
    const select = document.getElementById(`liveSymbol${i}`);
    if (!select) continue;
    const before = select.value;
    select.innerHTML = '<option value="">Selecione...</option>';
    enabled.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.id;
      opt.textContent = `${s.name} · ${s.category}`;
      select.appendChild(opt);
    });
    if (enabled.some(s => s.id === before)) {
      select.value = before;
      refreshEnhancedSelect(select);
      continue;
    }
    const preferred = LIVE_PREFERENCES[i - 1].find(id => enabled.some(s => s.id === id));
    if (preferred) select.value = preferred;
    refreshEnhancedSelect(select);
  }
}

function receiveSnapshot(snapshot) {
  if (!snapshot?.terminal?.id) return;
  snapshots[snapshot.terminal.id] = snapshot;
  const selected = document.getElementById('snapshotTerminal').value;
  if (!selected || selected === snapshot.terminal.id) renderSnapshot(snapshot);
}

function renderSelectedSnapshot() {
  const terminalId = document.getElementById('snapshotTerminal').value;
  renderSnapshot(snapshots[terminalId]);
}

function renderSnapshot(snapshot) {
  const status = document.getElementById('snapshotStatus');
  const table = document.getElementById('ticksTable');
  if (!snapshot) {
    status.textContent = 'Sem snapshot para o terminal selecionado. Inicie a leitura.';
    table.innerHTML = '';
    return;
  }

  const conn = snapshot.status || {};
  status.innerHTML = `
    <strong>${escapeHtml(snapshot.terminal?.label || snapshot.terminal?.id || '')}</strong><br>
    Status: ${conn.ok ? 'conectado permanentemente' : 'desconectado'} · ${escapeHtml(conn.message || '')}<br>
    Conta: ${escapeHtml(conn.account_login || '-')} · Servidor: ${escapeHtml(conn.server || '-')}<br>
    Última atualização: ${escapeHtml(snapshot.timestamp || '')}
  `;

  const ticks = snapshot.ticks || [];
  if (!ticks.length) {
    table.innerHTML = '<div class="muted">Nenhum tick recebido. Verifique o login e os símbolos disponíveis nessa corretora.</div>';
    return;
  }

  table.innerHTML = `
    <table>
      <thead><tr><th>Ativo</th><th>Símbolo</th><th>Bid</th><th>Ask</th><th>Spread</th><th>Hora</th></tr></thead>
      <tbody>
        ${ticks.map(t => `
          <tr>
            <td>${escapeHtml(t.name || t.logical_id || '')}</td>
            <td>${escapeHtml(t.symbol || '')}</td>
            <td>${formatNumber(t.bid)}</td>
            <td>${formatNumber(t.ask)}</td>
            <td>${formatNumber(t.spread)}</td>
            <td>${escapeHtml(t.time || '')}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function applyLiveStreams(payload) {
  liveStreams = payload || {};
  LIVE_SLOT_IDS.forEach(slotId => {
    const row = liveStreams[slotId];
    const active = Boolean(row?.config) && !['stopped', 'worker_stopped'].includes(row?.status?.state);
    if (active && row?.tick) liveTicks[slotId] = row.tick;
    else if (!active || !row?.tick) delete liveTicks[slotId];
  });
  for (let i = 1; i <= 3; i++) renderLiveSlot(i);
  updateLiveProof();
}

function receiveLiveTick(tick) {
  if (!tick?.slot_id) return;
  const stream = liveStreams[tick.slot_id];
  if (!stream?.config || ['stopped', 'worker_stopped'].includes(stream?.status?.state)) return;
  const previous = liveTicks[tick.slot_id];
  liveTicks[tick.slot_id] = tick;
  const slotNumber = Number(String(tick.slot_id).split('-').pop());
  if (slotNumber >= 1 && slotNumber <= 3) {
    renderLiveSlot(slotNumber);
    if (tick.changed && (!previous || previous.bid !== tick.bid || previous.ask !== tick.ask)) {
      const card = document.getElementById(`liveCard${slotNumber}`);
      card.classList.add('flash');
      setTimeout(() => card.classList.remove('flash'), 160);
    }
  }
  updateLiveProof();
}

function renderLiveSlot(slotNumber) {
  const slotId = `live-${slotNumber}`;
  const row = liveStreams[slotId] || {};
  const status = row.status || {};
  const tick = liveTicks[slotId] || row.tick || null;
  const card = document.getElementById(`liveCard${slotNumber}`);
  const dot = document.getElementById(`liveDot${slotNumber}`);
  const statusEl = document.getElementById(`liveStatus${slotNumber}`);
  const bidEl = document.getElementById(`liveBid${slotNumber}`);
  const askEl = document.getElementById(`liveAsk${slotNumber}`);
  const metaEl = document.getElementById(`liveMeta${slotNumber}`);
  if (!card) return;

  const terminalId = tick?.terminal_id || status?.terminal_id || row.config?.terminal_id;
  const worker = workerStates[terminalId] || {};
  const receivedAge = tick ? ageSeconds(tick.received_at) : Infinity;
  const liveOk = Boolean(tick && worker.connected && receivedAge < 2.0);
  const stale = Boolean(tick && worker.connected && receivedAge >= 2.0);
  const hasError = Boolean(status?.state === 'symbol_not_found' || status?.state === 'error' || (!worker.connected && terminalId));

  card.classList.toggle('streaming', liveOk);
  card.classList.toggle('stale', stale && !hasError);
  card.classList.toggle('error', hasError);
  dot.className = `status-dot ${liveOk ? 'ok' : (stale ? 'warn' : (hasError ? 'bad' : ''))}`;

  const terminalLabel = tick?.terminal_label || status?.terminal_label || row.config?.terminal_label || 'Terminal não selecionado';
  const symbolName = tick?.name || status?.name || row.config?.symbol?.name || '';
  const actualSymbol = tick?.resolved_symbol || status?.symbol || tick?.symbol || '';
  const message = status?.message || (tick ? 'Recebendo consultas do worker.' : 'Não iniciado.');
  setHtmlIfChanged(statusEl, `<strong>${escapeHtml(terminalLabel)}</strong>${symbolName ? ` · ${escapeHtml(symbolName)}` : ''}<br>${escapeHtml(message)}${actualSymbol ? ` · símbolo ${escapeHtml(actualSymbol)}` : ''}`);

  setTextIfChanged(bidEl, tick ? formatNumber(tick.bid) : '—');
  setTextIfChanged(askEl, tick ? formatNumber(tick.ask) : '—');

  if (!tick) {
    setHtmlIfChanged(metaEl, `PID worker: ${escapeHtml(worker.pid || status?.pid || '—')}<br>Conta/servidor: ${escapeHtml(worker.account_login || '—')} · ${escapeHtml(worker.server || '—')}<br>Leituras: 0 · ticks novos: 0`);
    return;
  }

  setHtmlIfChanged(metaEl, `
    <strong>PID worker:</strong> ${escapeHtml(tick.pid || worker.pid || '—')}<br>
    <strong>Conta/servidor:</strong> ${escapeHtml(tick.account_login || worker.account_login || '—')} · ${escapeHtml(tick.server || worker.server || '—')}<br>
    <strong>Spread:</strong> ${formatNumber(tick.spread)}<br>
    <strong>Leituras:</strong> ${escapeHtml(tick.poll_sequence || 0)} · <strong>ticks novos:</strong> ${escapeHtml(tick.tick_sequence || 0)}<br>
    <strong>Tick do mercado:</strong> ${escapeHtml(tick.time || '—')} (${formatAge(ageSeconds(tick.time))})<br>
    <strong>Última leitura:</strong> ${escapeHtml(tick.received_at || '—')} (${formatAge(receivedAge)})
  `);
}

function updateLiveProof() {
  const el = document.getElementById('liveProof');
  const ticks = LIVE_SLOT_IDS.map(id => liveTicks[id]).filter(Boolean);
  const uniqueTerminals = new Set(ticks.map(t => t.terminal_id).filter(Boolean));
  const activelyPolled = ticks.filter(t => ageSeconds(t.received_at) < 2.0);
  const connectedWorkers = terminals.filter(t => (workerStates[t.id] || t.worker || {}).connected).length;

  el.classList.remove('ok', 'warn');
  if (activelyPolled.length === 3 && uniqueTerminals.size === 3) {
    el.classList.add('ok');
    setHtmlIfChanged(el, `<strong>✓ Simultaneidade confirmada:</strong> 3 fluxos recentes, vindos de 3 terminais e PIDs independentes. Workers conectados: ${connectedWorkers}.`);
  } else if (ticks.length) {
    el.classList.add('warn');
    setHtmlIfChanged(el, `<strong>Teste em andamento:</strong> ${activelyPolled.length}/3 fluxos recentes · ${uniqueTerminals.size} terminal(is) distinto(s) · ${connectedWorkers} worker(s) conectado(s).`);
  } else {
    setTextIfChanged(el, `Configure os painéis para comprovar as conexões simultâneas. Workers conectados: ${connectedWorkers}.`);
  }
}

async function loadRuntimeLimits() {
  if (!bridge?.getRuntimeLimits) return;
  const res = parseResponse(await bridge.getRuntimeLimits());
  if (res.ok && res.data) runtimeLimits = { ...runtimeLimits, ...res.data };
}

async function loadTerminals() {
  if (!bridge) return;
  await loadRuntimeLimits();
  const res = parseResponse(await bridge.getTerminals());
  if (!res.ok) return toast(res.message, true);
  renderTerminals(res.data);
}

async function loadWorkerStates() {
  if (!bridge) return;
  const res = parseResponse(await bridge.getWorkerStates());
  if (res.ok) applyWorkerStates(res.data);
}

async function loadSnapshots() {
  if (!bridge) return;
  const res = parseResponse(await bridge.getSnapshots());
  if (!res.ok) return;
  snapshots = res.data || {};
  renderSelectedSnapshot();
}

async function loadLiveStreams() {
  if (!bridge) return;
  const res = parseResponse(await bridge.getLiveStreams());
  if (res.ok) applyLiveStreams(res.data);
}

async function loadSymbols() {
  if (!bridge) return;
  const res = parseResponse(await bridge.getSymbols());
  if (!res.ok) return toast(res.message, true);
  renderSymbols(res.data);
}

async function loadBaseMt5Status() {
  if (!bridge) return;
  const res = parseResponse(await bridge.getBaseMt5Status());
  const el = document.getElementById('mt5BaseStatus');
  if (!res.ok) {
    el.className = 'base-status bad';
    el.textContent = res.message;
    return;
  }
  const data = res.data || {};
  el.className = `base-status ${data.ok ? 'ok' : 'bad'}`;
  el.innerHTML = data.ok
    ? `<strong>Base MT5 pronta.</strong><span>${escapeHtml(data.path || '')}</span>`
    : `<strong>Base MT5 indisponível.</strong><span>${escapeHtml(data.message || '')}</span><span>Ausentes: ${escapeHtml((data.missing || []).join(', '))}</span><span>Pasta esperada: ${escapeHtml(data.path || '')}</span>`;
  document.getElementById('btnCreateTerminal').disabled = !data.ok;
}

async function createTerminal() {
  const label = document.getElementById('terminalLabel').value;
  const broker = document.getElementById('brokerName').value;
  const login = document.getElementById('accountLogin').value;
  const res = parseResponse(await bridge.createTerminal(label, broker, login));
  toast(res.message, !res.ok);
  if (res.ok) {
    document.getElementById('terminalLabel').value = '';
    document.getElementById('brokerName').value = '';
    document.getElementById('accountLogin').value = '';
    await loadTerminals();
  }
}

function openEditTerminal(id) {
  const terminal = terminals.find(t => t.id === id);
  if (!terminal) return toast('Terminal não encontrado.', true);
  document.getElementById('editTerminalId').value = terminal.id;
  document.getElementById('editTerminalLabel').value = terminal.label || '';
  document.getElementById('editBrokerName').value = terminal.broker_name || '';
  document.getElementById('editAccountLogin').value = terminal.account_login || '';
  const worker = workerStates[terminal.id] || terminal.worker || {};
  document.getElementById('editTerminalDetails').innerHTML = `
    <strong>Instância controlada</strong>
    <span>${escapeHtml(terminal.instance_dir || '')}</span>
    <span>Conta detectada: ${escapeHtml(worker.account_login || 'ainda não detectada')}</span>
    <span>Servidor detectado: ${escapeHtml(worker.server || 'ainda não detectado')}</span>
  `;
  document.getElementById('editTerminalModal').classList.remove('hidden');
  document.getElementById('editTerminalLabel').focus();
}

function closeEditTerminal() {
  document.getElementById('editTerminalModal').classList.add('hidden');
}

async function saveTerminalEdit() {
  const id = document.getElementById('editTerminalId').value;
  const label = document.getElementById('editTerminalLabel').value;
  const broker = document.getElementById('editBrokerName').value;
  const login = document.getElementById('editAccountLogin').value;
  const button = document.getElementById('btnSaveTerminalEdit');
  button.disabled = true;
  button.textContent = 'Salvando...';
  try {
    const res = parseResponse(await bridge.updateTerminal(id, label, broker, login));
    toast(res.message, !res.ok);
    if (res.ok) {
      closeEditTerminal();
      await loadTerminals();
      await loadWorkerStates();
    }
  } finally {
    button.disabled = false;
    button.textContent = 'Salvar alterações';
  }
}

function openDeleteTerminal(id) {
  const terminal = terminals.find(t => t.id === id);
  if (!terminal) return toast('Terminal não encontrado.', true);
  if (terminal.running) {
    return toast('Feche o MT5 antes de excluir esta instância.', true);
  }
  document.getElementById('deleteTerminalId').value = terminal.id;
  document.getElementById('deleteTerminalConfirmation').value = '';
  document.getElementById('btnConfirmDeleteTerminal').disabled = true;
  document.getElementById('deleteTerminalDetails').innerHTML = `
    <strong>${escapeHtml(terminal.label || terminal.id)}</strong>
    <span>Corretora: ${escapeHtml(terminal.broker_name || '—')}</span>
    <span>Conta: ${escapeHtml(terminal.account_login || '—')}</span>
    <span>Pasta local: ${escapeHtml(terminal.instance_dir || '—')}</span>
  `;
  document.getElementById('deleteTerminalModal').classList.remove('hidden');
  document.getElementById('deleteTerminalConfirmation').focus();
}

function closeDeleteTerminal() {
  document.getElementById('deleteTerminalModal').classList.add('hidden');
}

function updateDeleteConfirmationState() {
  const value = document.getElementById('deleteTerminalConfirmation').value.trim().toUpperCase();
  document.getElementById('btnConfirmDeleteTerminal').disabled = value !== 'EXCLUIR';
}

async function confirmDeleteTerminal() {
  const id = document.getElementById('deleteTerminalId').value;
  const confirmation = document.getElementById('deleteTerminalConfirmation').value;
  const button = document.getElementById('btnConfirmDeleteTerminal');
  button.disabled = true;
  button.textContent = 'Excluindo...';
  try {
    const res = parseResponse(await bridge.deleteTerminal(id, confirmation));
    toast(res.message, !res.ok);
    if (res.ok) {
      delete workerStates[id];
      delete snapshots[id];
      selectedTerminalIds.delete(id);
      persistSelectedTerminals();
      Object.keys(liveTicks).forEach(slotId => {
        if (liveTicks[slotId]?.terminal_id === id) delete liveTicks[slotId];
      });
      closeDeleteTerminal();
      await loadTerminals();
      await loadWorkerStates();
      await loadLiveStreams();
    }
  } finally {
    button.textContent = 'Excluir terminal';
    updateDeleteConfirmationState();
  }
}

async function launchTerminal(id) {
  const res = parseResponse(await bridge.launchTerminal(id));
  toast(res.message, !res.ok);
  await loadWorkerStates();
  await loadTerminals();
}

async function stopTerminal(id) {
  const res = parseResponse(await bridge.stopTerminal(id));
  toast(res.message, !res.ok);
  await loadWorkerStates();
  await loadTerminals();
  await loadLiveStreams();
}

async function toggleReading(id) {
  const res = parseResponse(await bridge.toggleWorker(id));
  toast(res.message, !res.ok);
  await loadWorkerStates();
  await loadTerminals();
  await loadLiveStreams();
}

async function reconnectWorker(id) {
  const res = parseResponse(await bridge.reconnectWorker(id));
  toast(res.message, !res.ok);
}

async function openSelectedTerminals() {
  const ids = selectedTerminalList();
  if (!ids.length) return toast('Selecione pelo menos um terminal.', true);
  const res = parseResponse(await bridge.startSelectedWorkers(JSON.stringify(ids)));
  toast(res.message, !res.ok);
  await loadWorkerStates();
  await loadTerminals();
}

async function closeSelectedTerminals() {
  const ids = selectedTerminalList();
  if (!ids.length) return toast('Selecione pelo menos um terminal.', true);
  const res = parseResponse(await bridge.closeSelectedTerminals(JSON.stringify(ids)));
  toast(res.message, !res.ok);
  if (res.ok) {
    Object.keys(liveTicks).forEach(slotId => {
      if (ids.includes(liveTicks[slotId]?.terminal_id)) delete liveTicks[slotId];
    });
  }
  await loadWorkerStates();
  await loadTerminals();
  await loadLiveStreams();
}

async function stopAllWorkers() {
  const res = parseResponse(await bridge.stopAllWorkers());
  toast(res.message, !res.ok);
  await loadWorkerStates();
  await loadTerminals();
}

async function refreshSnapshot(id) {
  const terminalId = id || document.getElementById('snapshotTerminal').value;
  if (!terminalId) return toast('Selecione um terminal.', true);
  const res = parseResponse(await bridge.refreshSnapshot(terminalId));
  toast(res.message, !res.ok);
  if (res.ok && res.data) receiveSnapshot(res.data);
}

async function startLiveSlot(slotNumber, quiet = false) {
  const terminalId = document.getElementById(`liveTerminal${slotNumber}`).value;
  const symbolId = document.getElementById(`liveSymbol${slotNumber}`).value;
  if (!terminalId || !symbolId) {
    if (!quiet) toast(`Selecione terminal e ativo no Fluxo ${slotNumber}.`, true);
    return false;
  }
  const res = parseResponse(await bridge.configureLiveStream(`live-${slotNumber}`, terminalId, symbolId));
  if (!quiet) toast(res.message, !res.ok);
  if (res.ok) await loadLiveStreams();
  return Boolean(res.ok);
}

async function stopLiveSlot(slotNumber, quiet = false) {
  const res = parseResponse(await bridge.clearLiveStream(`live-${slotNumber}`));
  delete liveTicks[`live-${slotNumber}`];
  if (!quiet) toast(res.message, !res.ok);
  await loadLiveStreams();
  renderLiveSlot(slotNumber);
  return Boolean(res.ok);
}

async function startAllLiveSlots() {
  const choices = [1, 2, 3].map(i => ({
    terminal: document.getElementById(`liveTerminal${i}`).value,
    symbol: document.getElementById(`liveSymbol${i}`).value
  }));
  if (choices.some(row => !row.terminal || !row.symbol)) {
    return toast('Preencha terminal e ativo nos três fluxos.', true);
  }
  const distinct = new Set(choices.map(row => row.terminal));
  if (distinct.size < 3) {
    toast('Os fluxos serão iniciados, mas use três terminais diferentes para comprovar três conexões simultâneas.', true);
  }
  const results = [];
  for (let i = 1; i <= 3; i++) results.push(await startLiveSlot(i, true));
  toast(results.every(Boolean) ? 'Três fluxos configurados. Aguarde as leituras.' : 'Um ou mais fluxos não puderam ser iniciados.', !results.every(Boolean));
}

async function stopAllLiveSlots() {
  const res = parseResponse(await bridge.clearAllLiveStreams());
  liveTicks = {};
  toast(res.message, !res.ok);
  await loadLiveStreams();
  for (let i = 1; i <= 3; i++) renderLiveSlot(i);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
}

function escapeJs(value) {
  return String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('pt-BR', { maximumFractionDigits: 8 });
}

function ageSeconds(value) {
  if (!value) return Infinity;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return Infinity;
  return Math.max(0, (Date.now() - parsed) / 1000);
}

function formatAge(seconds) {
  if (!Number.isFinite(seconds)) return 'sem horário';
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms atrás`;
  if (seconds < 60) return `${seconds.toFixed(1)} s atrás`;
  return `${Math.floor(seconds / 60)} min atrás`;
}

window.addEventListener('DOMContentLoaded', () => {
  restoreSelectedTerminals();
  enhanceAllSelects();
  document.addEventListener('click', () => closeEnhancedSelects());
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      closeEnhancedSelects();
      closeEditTerminal();
      closeDeleteTerminal();
    }
  });
  document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => switchView(btn.dataset.view)));
  document.getElementById('btnCreateTerminal').addEventListener('click', createTerminal);
  document.getElementById('btnSaveTerminalEdit').addEventListener('click', saveTerminalEdit);
  document.querySelectorAll('[data-close-edit]').forEach(el => el.addEventListener('click', closeEditTerminal));
  document.querySelectorAll('[data-close-delete]').forEach(el => el.addEventListener('click', closeDeleteTerminal));
  document.getElementById('deleteTerminalConfirmation').addEventListener('input', updateDeleteConfirmationState);
  document.getElementById('btnConfirmDeleteTerminal').addEventListener('click', confirmDeleteTerminal);
  document.getElementById('btnReloadTerminals').addEventListener('click', loadTerminals);
  document.getElementById('btnReloadSymbols').addEventListener('click', loadSymbols);
  document.getElementById('btnOpenSelected').addEventListener('click', openSelectedTerminals);
  document.getElementById('btnCloseSelected').addEventListener('click', closeSelectedTerminals);
  document.getElementById('btnStopAll').addEventListener('click', stopAllWorkers);
  document.getElementById('btnSnapshot').addEventListener('click', () => refreshSnapshot());
  document.getElementById('snapshotTerminal').addEventListener('change', renderSelectedSnapshot);
  document.getElementById('btnStartLiveAll').addEventListener('click', startAllLiveSlots);
  document.getElementById('btnStopLiveAll').addEventListener('click', stopAllLiveSlots);
  document.querySelectorAll('[data-live-start]').forEach(btn => btn.addEventListener('click', () => startLiveSlot(Number(btn.dataset.liveStart))));
  document.querySelectorAll('[data-live-stop]').forEach(btn => btn.addEventListener('click', () => stopLiveSlot(Number(btn.dataset.liveStop))));

  setInterval(() => {
    for (let i = 1; i <= 3; i++) renderLiveSlot(i);
    updateLiveProof();
  }, 1000);

  new QWebChannel(qt.webChannelTransport, channel => {
    bridge = channel.objects.marketHub;
    bridge.terminalsChanged.connect(text => renderTerminals(JSON.parse(text)));
    bridge.workerStatesChanged.connect(text => applyWorkerStates(JSON.parse(text)));
    bridge.snapshotChanged.connect(text => receiveSnapshot(JSON.parse(text)));
    bridge.liveTickChanged.connect(text => receiveLiveTick(JSON.parse(text)));
    bridge.liveStreamStatusChanged.connect(text => applyLiveStreams(JSON.parse(text)));
    setBridgeStatus(true, 'Ponte Python conectada');
    loadBaseMt5Status();
    loadTerminals();
    loadSymbols();
    loadWorkerStates();
    loadSnapshots();
    loadLiveStreams();
  });
});
