let bridge = null;
let terminals = [];
let symbols = [];
let workerStates = {};
let snapshots = {};
let liveStreams = {};
let liveTicks = {};
let runtimeLimits = { max_active_mt5: null, registered: 0, open_mt5: 0, active_workers: 0 };
let workerSummarySignature = '';
let dashboardTerminalSignature = '';
const SELECTED_TERMINALS_KEY = 'ep_market_hub_selected_terminals_v1';
let selectedTerminalIds = new Set();
let bulkCloseInProgress = false;

const LIVE_SLOT_IDS = ['live-1', 'live-2', 'live-3'];
const LIVE_PREFERENCES = [
  ['eurusd', 'usdjpy', 'bitcoin'],
  ['bitcoin', 'usdjpy', 'eurusd'],
  ['winq26', 'wdo', 'win', 'bitcoin', 'eurusd']
];

function activeTerminalLimit() {
  const value = Number(runtimeLimits.max_active_mt5);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

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

function terminalBulkActionState(rows, selectedIds, states, maxActive) {
  maxActive = Number.isInteger(Number(maxActive)) && Number(maxActive) > 0
    ? Number(maxActive)
    : 0;
  const selected = rows.filter(t => selectedIds.has(t.id));
  const openCount = rows.filter(t => t.running).length;
  const activeWorkerCount = rows.filter(t => (states[t.id] || t.worker || {}).alive).length;
  const candidates = selected.filter(t => {
    const worker = states[t.id] || t.worker || {};
    return !t.running || !worker.alive;
  });
  const closeCandidates = selected.filter(t => {
    const worker = states[t.id] || t.worker || {};
    return t.running || worker.alive;
  });
  const neededOpenSlots = candidates.filter(t => !t.running).length;
  const neededWorkerSlots = candidates.filter(t => !(states[t.id] || t.worker || {}).alive).length;
  const availableOpenSlots = Math.max(0, maxActive - openCount);
  const availableWorkerSlots = Math.max(0, maxActive - activeWorkerCount);
  const exceedsCapacity = neededOpenSlots > availableOpenSlots
    || neededWorkerSlots > availableWorkerSlots;

  let openTitle = 'Abre os MT5 selecionados e inicia suas leituras';
  if (!selected.length) openTitle = 'Selecione pelo menos um terminal';
  else if (!candidates.length) openTitle = 'Todos os terminais selecionados já estão abertos com leitura ativa';
  else if (!maxActive) openTitle = 'O limite simultâneo do kernel ainda não foi carregado';
  else if (exceedsCapacity) openTitle = `Não há vagas para abrir toda a seleção (limite de ${maxActive} MT5)`;

  return {
    openDisabled: candidates.length === 0 || exceedsCapacity,
    closeDisabled: closeCandidates.length === 0,
    openTitle,
    closeTitle: closeCandidates.length
      ? 'Fecha os MT5 selecionados e encerra suas leituras'
      : 'Nenhum terminal aberto ou com leitura ativa está selecionado',
  };
}

function updateSelectionUi() {
  const maxActive = activeTerminalLimit();
  const validIds = new Set(terminals.map(t => t.id));
  selectedTerminalIds = new Set(Array.from(selectedTerminalIds).filter(id => validIds.has(id)).slice(0, maxActive));
  persistSelectedTerminals();

  document.querySelectorAll('[data-terminal-select]').forEach(input => {
    const checked = selectedTerminalIds.has(input.dataset.terminalSelect);
    if (input.checked !== checked) input.checked = checked;
    input.closest('.terminal-item')?.classList.toggle('selected', checked);
    const terminal = terminals.find(item => item.id === input.dataset.terminalSelect);
    input.disabled = bulkCloseInProgress || !terminalInstanceReady(terminal);
  });

  const count = selectedTerminalIds.size;
  setTextIfChanged(document.getElementById('selectionStatus'), `Selecionados: ${count} de ${maxActive}`);
  const openButton = document.getElementById('btnOpenSelected');
  const closeButton = document.getElementById('btnCloseSelected');
  const state = terminalBulkActionState(terminals, selectedTerminalIds, workerStates, maxActive);
  if (openButton) {
    openButton.disabled = state.openDisabled || bulkCloseInProgress;
    openButton.title = state.openTitle;
  }
  if (closeButton) {
    closeButton.disabled = state.closeDisabled || bulkCloseInProgress;
    closeButton.title = state.closeTitle;
  }
}

function toggleTerminalSelection(terminalId, checked) {
  if (bulkCloseInProgress) return;
  const maxActive = activeTerminalLimit();
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
    stopped: 'desconectado',
    starting: 'iniciando',
    connected: 'conectado',
    waiting_login: 'aguardando login',
    reopening_terminal: 'reabrindo MT5',
    reconnecting: 'reconectando',
    error: 'erro'
  };
  return labels[state] || state || 'desconectado';
}

function workerBadgeClass(worker) {
  if (worker?.connected) return 'ok';
  if (worker?.state === 'waiting_login' || worker?.state === 'reopening_terminal' || worker?.state === 'reconnecting' || worker?.state === 'starting') return 'warn';
  if (worker?.state === 'error') return 'bad';
  return '';
}

function terminalInstanceState(terminal) {
  return terminal?.instance_status?.state || 'ready';
}

function terminalInstanceReady(terminal) {
  return terminalInstanceState(terminal) === 'ready';
}

function terminalProcessLabel(terminal, worker) {
  const instanceState = terminalInstanceState(terminal);
  if (instanceState === 'directory_missing') return 'Instância ausente';
  if (instanceState === 'executable_missing') return 'Executável ausente';
  if (instanceState === 'invalid_path') return 'Caminho inválido';
  if (worker?.state === 'reopening_terminal') return 'Reabrindo MT5';
  return `MT5 ${terminal?.running ? 'aberto' : 'fechado'}`;
}

function terminalProcessBadgeClass(terminal, worker) {
  if (!terminalInstanceReady(terminal)) return 'bad';
  if (worker?.state === 'reopening_terminal') return 'warn';
  return terminal?.running ? 'ok' : '';
}

function terminalActionState(terminal, worker, openCount, activeWorkerCount, maxActive) {
  const workerAlive = Boolean(worker?.alive);
  const instanceReady = terminalInstanceReady(terminal);
  const capacityUnavailable = !maxActive
    || openCount >= maxActive
    || activeWorkerCount >= maxActive;
  const openBlocked = !instanceReady || (!terminal.running && capacityUnavailable);
  const readingBlocked = workerAlive
    ? false
    : (!instanceReady || !terminal.running || !maxActive || activeWorkerCount >= maxActive);
  const readingLabel = workerAlive
    ? (worker.connected ? 'Parar leitura' : 'Parar tentativa')
    : 'Iniciar leitura';
  const readingTitle = workerAlive
    ? 'Encerra o processo de leitura deste terminal'
    : (!terminal.running
        ? 'Abra o MT5 para habilitar a leitura'
        : ((!maxActive || activeWorkerCount >= maxActive)
            ? `Limite de ${maxActive || '—'} MT5 simultâneos atingido`
            : ''));
  return {
    openBlocked,
    readingBlocked,
    readingLabel,
    readingTitle,
    editBlocked: Boolean(!instanceReady || terminal.running || workerAlive),
    deleteBlocked: Boolean(terminal.running || workerAlive),
  };
}

function renderTerminals(rows) {
  terminals = (rows || []).slice().sort(compareTerminal);
  const selectedCountBeforeHealthCheck = selectedTerminalIds.size;
  terminals.forEach(terminal => {
    if (!terminalInstanceReady(terminal)) selectedTerminalIds.delete(terminal.id);
  });
  if (selectedTerminalIds.size !== selectedCountBeforeHealthCheck) persistSelectedTerminals();
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
  const maxActive = activeTerminalLimit();
  const openCount = terminals.filter(t => t.running).length;
  const activeWorkerCount = terminals.filter(t => (workerStates[t.id] || t.worker || {}).alive).length;
  list.innerHTML = terminals.map(t => {
    const worker = workerStates[t.id] || t.worker || {};
    const actionState = terminalActionState(
      t,
      worker,
      openCount,
      activeWorkerCount,
      maxActive,
    );
    const limitTitle = `Limite de ${maxActive} MT5 simultâneos atingido`;
    const selected = selectedTerminalIds.has(t.id);
    const instanceReady = terminalInstanceReady(t);
    const instanceMessage = t.instance_status?.message || 'Instância local indisponível.';
    const deleteAction = instanceReady ? 'openDeleteTerminal' : 'openInstanceResolution';
    return `
      <div class="terminal-item ${selected ? 'selected' : ''}" id="terminalItem-${escapeHtml(t.id)}" data-terminal-id="${escapeHtml(t.id)}">
        <div class="terminal-select-row">
          <label class="terminal-select-label">
            <input type="checkbox" data-terminal-select="${escapeHtml(t.id)}" ${selected ? 'checked' : ''} ${instanceReady ? '' : 'disabled'}
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
            <span class="badge mt5-badge ${terminalProcessBadgeClass(t, worker)}">${escapeHtml(terminalProcessLabel(t, worker))}</span>
            <span class="badge worker-badge ${workerBadgeClass(worker)}">${escapeHtml(workerLabel(worker.state))}</span>
          </div>
        </div>
        <small class="muted path">${escapeHtml(t.terminal_exe || '')}</small>
        <div class="worker-detail">${terminalWorkerDetailHtml(worker, t)}</div>
        <div class="actions">
          <button data-role="edit-button" onclick="openEditTerminal('${escapeJs(t.id)}')"
                  ${actionState.editBlocked ? 'disabled' : ''}
                  title="${actionState.editBlocked ? 'Feche o MT5 e pare a leitura antes de editar' : 'Edita o cadastro do terminal'}">Editar</button>
          <button data-role="open-button" onclick="launchTerminal('${escapeJs(t.id)}')"
                  ${t.running || actionState.openBlocked ? 'disabled' : ''}
                  title="${!instanceReady ? escapeHtml(instanceMessage) : (t.running ? 'MT5 já está aberto' : (actionState.openBlocked ? limitTitle : 'Abre o MT5 e inicia a leitura'))}">Abrir MT5</button>
          <button data-role="reading-button" class="${worker.connected ? '' : 'success'}"
                  onclick="toggleReading('${escapeJs(t.id)}')" ${actionState.readingBlocked ? 'disabled' : ''}
                  title="${actionState.readingTitle}">${actionState.readingLabel}</button>
          <button data-role="reconnect-button" onclick="reconnectWorker('${escapeJs(t.id)}')" ${worker.connected ? '' : 'disabled'}>Reconectar</button>
          <button data-role="close-button" class="danger" onclick="stopTerminal('${escapeJs(t.id)}')" ${t.running ? '' : 'disabled'}>Fechar MT5</button>
          <button data-role="delete-button" class="danger" onclick="${deleteAction}('${escapeJs(t.id)}')" ${actionState.deleteBlocked ? 'disabled' : ''} title="${actionState.deleteBlocked ? 'Feche o MT5 e pare a leitura antes de resolver a instância' : (instanceReady ? 'Exclui o cadastro e a pasta local da instância' : escapeHtml(instanceMessage))}">${instanceReady ? 'Excluir' : 'Resolver'}</button>
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

function terminalWorkerDetailHtml(worker, terminal = null) {
  const instanceMessage = terminal && !terminalInstanceReady(terminal)
    ? `<span>${escapeHtml(terminal.instance_status?.message || 'Instância local indisponível.')}</span>`
    : '';
  return `
    ${instanceMessage}
    <span>${escapeHtml(worker?.message || 'Desconectado.')}</span>
    ${worker?.pid ? `<span>PID worker: ${escapeHtml(worker.pid)}</span>` : ''}
    ${worker?.account_login ? `<span>Conta conectada: ${escapeHtml(worker.account_login)} · ${escapeHtml(worker.server || '')}</span>` : ''}
    ${worker?.last_snapshot ? `<span>Último snapshot: ${escapeHtml(worker.last_snapshot)}</span>` : ''}
  `;
}

function updateTerminalWorkerRows() {
  const maxActive = activeTerminalLimit();
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
    const mt5Badge = item.querySelector('.mt5-badge');
    if (mt5Badge) {
      mt5Badge.className = `badge mt5-badge ${terminalProcessBadgeClass(t, worker)}`;
      setTextIfChanged(mt5Badge, terminalProcessLabel(t, worker));
    }
    setHtmlIfChanged(item.querySelector('.worker-detail'), terminalWorkerDetailHtml(worker, t));

    const editButton = item.querySelector('[data-role="edit-button"]');
    if (editButton) {
      const blocked = Boolean(!terminalInstanceReady(t) || t.running || worker.alive);
      editButton.disabled = blocked;
      editButton.title = blocked
        ? (!terminalInstanceReady(t)
            ? (t.instance_status?.message || 'Resolva a instância local antes de editar')
            : 'Feche o MT5 e pare a leitura antes de editar')
        : 'Edita o cadastro do terminal';
    }

    const actionState = terminalActionState(
      t,
      worker,
      openCount,
      activeWorkerCount,
      maxActive,
    );
    const limitTitle = `Limite de ${maxActive} MT5 simultâneos atingido`;
    const openButton = item.querySelector('[data-role="open-button"]');
    if (openButton) {
      const blocked = actionState.openBlocked;
      openButton.disabled = Boolean(t.running || blocked);
      openButton.title = !terminalInstanceReady(t)
        ? (t.instance_status?.message || 'Instância local indisponível')
        : (t.running
        ? 'MT5 já está aberto'
        : (blocked ? limitTitle : 'Abre o MT5 e inicia a leitura'));
    }

    const readingButton = item.querySelector('[data-role="reading-button"]');
    if (readingButton) {
      readingButton.disabled = actionState.readingBlocked;
      readingButton.classList.toggle('success', !worker.connected);
      setTextIfChanged(readingButton, actionState.readingLabel);
      readingButton.title = actionState.readingTitle;
    }

    const reconnectButton = item.querySelector('[data-role="reconnect-button"]');
    if (reconnectButton) reconnectButton.disabled = !worker.connected;
    const closeButton = item.querySelector('[data-role="close-button"]');
    if (closeButton) closeButton.disabled = !t.running;
    const deleteButton = item.querySelector('[data-role="delete-button"]');
    if (deleteButton) {
      deleteButton.disabled = actionState.deleteBlocked;
      deleteButton.title = actionState.deleteBlocked
        ? 'Feche o MT5 e pare a leitura antes de excluir a instância'
        : 'Exclui o cadastro e a pasta local da instância';
    }
  });
}

function applyWorkerStates(rows) {
  (rows || []).forEach(row => { workerStates[row.terminal_id] = row; });
  terminals = terminals.map(t => ({ ...t, worker: workerStates[t.id] || t.worker }));
  updateTerminalWorkerRows();
  updateSelectionUi();
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
  const limit = activeTerminalLimit();
  el.textContent = `Leituras: ${connected} conectadas · ${alive}/${limit || '—'} ativas`;
  el.classList.toggle('ok', connected > 0);
  const stopAllButton = document.getElementById('btnStopAll');
  stopAllButton.disabled = alive === 0;
  stopAllButton.title = alive === 0
    ? 'Nenhuma leitura ativa para encerrar'
    : `Encerra as ${alive} leitura(s) ativa(s)`;
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

function liveTerminalSelection(row, before, selectionTouched, connected, fallbackIndex) {
  const configuredId = String(row?.config?.terminal_id || '');
  const connectedIds = new Set(connected.map(t => t.id));
  if (selectionTouched && connectedIds.has(before)) return before;
  if (configuredId) return configuredId;
  if (connectedIds.has(before)) return before;
  return connected[fallbackIndex]?.id || '';
}

function populateLiveTerminalSelectors() {
  const connected = connectedTerminals();
  for (let i = 1; i <= 3; i++) {
    const select = document.getElementById(`liveTerminal${i}`);
    if (!select) continue;
    const before = select.value;
    const row = liveStreams[`live-${i}`] || {};
    const configuredId = String(row?.config?.terminal_id || '');
    const selectionTouched = select.dataset.selectionTouched === '1';
    select.innerHTML = '<option value="">Selecione um terminal conectado...</option>';
    connected.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = `${t.label || t.id}${t.broker_name ? ` · ${t.broker_name}` : ''}`;
      select.appendChild(opt);
    });

    if (configuredId && !connected.some(t => t.id === configuredId)) {
      const profile = terminals.find(t => t.id === configuredId);
      const opt = document.createElement('option');
      opt.value = configuredId;
      opt.disabled = true;
      opt.textContent = `${profile?.label || row.config?.terminal_label || configuredId} · MT5 fechado`;
      select.appendChild(opt);
    }

    select.value = liveTerminalSelection(row, before, selectionTouched, connected, i - 1);
    refreshEnhancedSelect(select);
    updateLiveSlotAction(i);
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
      updateLiveSlotAction(i);
      continue;
    }
    const preferred = LIVE_PREFERENCES[i - 1].find(id => enabled.some(s => s.id === id));
    if (preferred) select.value = preferred;
    refreshEnhancedSelect(select);
    updateLiveSlotAction(i);
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
  populateLiveTerminalSelectors();
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

function liveSlotActionState(row, terminalId, symbolId) {
  const configured = Boolean(row?.config);
  const selectionComplete = Boolean(terminalId && symbolId);
  if (!configured) {
    return { disabled: !selectionComplete, label: 'Iniciar', changed: false };
  }

  const configuredTerminal = String(row.config?.terminal_id || '');
  const configuredSymbol = String(row.config?.symbol?.id || '');
  const changed = selectionComplete
    && (terminalId !== configuredTerminal || symbolId !== configuredSymbol);
  return { disabled: !changed, label: changed ? 'Alterar' : 'Iniciar', changed };
}

function updateLiveSlotAction(slotNumber) {
  const button = document.querySelector(`[data-live-start="${slotNumber}"]`);
  const terminalId = document.getElementById(`liveTerminal${slotNumber}`)?.value || '';
  const symbolId = document.getElementById(`liveSymbol${slotNumber}`)?.value || '';
  if (!button) return;
  const state = liveSlotActionState(liveStreams[`live-${slotNumber}`] || {}, terminalId, symbolId);
  button.disabled = state.disabled;
  setTextIfChanged(button, state.label);
  button.title = state.changed
    ? 'Aplica o novo terminal ou ativo a este fluxo'
    : (state.disabled ? 'Esta configuração já está aplicada' : 'Inicia este fluxo');
}

function liveSlotIssueDetails(row, tick, slotNumber) {
  if (!row?.config && !tick) return null;
  const status = row?.status || {};
  if (['stopped', 'worker_stopped'].includes(status.state)) return null;
  const receivedAge = tick ? ageSeconds(tick.received_at) : Infinity;
  if (tick && receivedAge < 2.0) return null;
  return {
    slotNumber,
    terminalLabel: tick?.terminal_label || status.terminal_label || row?.config?.terminal_label || 'terminal não identificado',
    symbolName: tick?.name || status.name || row?.config?.symbol?.name || 'ativo não identificado',
    detail: tick
      ? `leitura atrasada; último retorno ${formatAge(receivedAge)}`
      : (status.message || 'aguardando a primeira leitura'),
  };
}

function liveSlotVisibleTick(row, cachedTick) {
  const state = row?.status?.state || '';
  if (['stopped', 'worker_stopped'].includes(state)) return null;
  return cachedTick || row?.tick || null;
}

function renderLiveSlot(slotNumber) {
  const slotId = `live-${slotNumber}`;
  const row = liveStreams[slotId] || {};
  const status = row.status || {};
  const tick = liveSlotVisibleTick(row, liveTicks[slotId]);
  const card = document.getElementById(`liveCard${slotNumber}`);
  const dot = document.getElementById(`liveDot${slotNumber}`);
  const statusEl = document.getElementById(`liveStatus${slotNumber}`);
  const bidEl = document.getElementById(`liveBid${slotNumber}`);
  const askEl = document.getElementById(`liveAsk${slotNumber}`);
  const metaEl = document.getElementById(`liveMeta${slotNumber}`);
  if (!card) return;

  const terminalId = tick?.terminal_id || status?.terminal_id || row.config?.terminal_id;
  const worker = workerStates[terminalId] || {};
  const terminal = terminals.find(t => t.id === terminalId);
  const stopped = ['stopped', 'worker_stopped'].includes(status?.state);
  const receivedAge = tick ? ageSeconds(tick.received_at) : Infinity;
  const liveOk = Boolean(tick && worker.connected && receivedAge < 2.0);
  const stale = Boolean(tick && worker.connected && receivedAge >= 2.0);
  const hasError = Boolean(!stopped && (
    status?.state === 'symbol_not_found'
    || status?.state === 'error'
    || (!worker.connected && terminalId)
  ));

  card.classList.toggle('streaming', liveOk);
  card.classList.toggle('stale', stale && !hasError);
  card.classList.toggle('error', hasError);
  dot.className = `status-dot ${liveOk ? 'ok' : (stale ? 'warn' : (hasError ? 'bad' : ''))}`;

  const terminalLabel = tick?.terminal_label || status?.terminal_label || row.config?.terminal_label || 'Terminal não selecionado';
  const symbolName = tick?.name || status?.name || row.config?.symbol?.name || '';
  const actualSymbol = tick?.resolved_symbol || status?.symbol || tick?.symbol || '';
  const message = stopped
    ? (terminal && !terminal.running ? 'Fluxo parado: MT5 fechado.' : 'Fluxo parado: leitura encerrada.')
    : (status?.message || (tick ? 'Recebendo consultas do worker.' : 'Não iniciado.'));
  setHtmlIfChanged(statusEl, `<strong>${escapeHtml(terminalLabel)}</strong>${symbolName ? ` · ${escapeHtml(symbolName)}` : ''}<br>${escapeHtml(message)}${actualSymbol ? ` · símbolo ${escapeHtml(actualSymbol)}` : ''}`);
  updateLiveSlotAction(slotNumber);

  setTextIfChanged(bidEl, tick ? formatNumber(tick.bid) : '—');
  setTextIfChanged(askEl, tick ? formatNumber(tick.ask) : '—');

  if (stopped) {
    setHtmlIfChanged(metaEl, 'Leitura encerrada.<br>Selecione outro terminal conectado para alterar este fluxo.');
    return;
  }

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
  const activeSlotIds = LIVE_SLOT_IDS.filter(id => {
    const row = liveStreams[id] || {};
    return Boolean(row.config) && !['stopped', 'worker_stopped'].includes(row?.status?.state);
  });
  const stoppedCount = LIVE_SLOT_IDS.filter(id => {
    const row = liveStreams[id] || {};
    return Boolean(row.config) && ['stopped', 'worker_stopped'].includes(row?.status?.state);
  }).length;
  const ticks = activeSlotIds.map(id => liveTicks[id]).filter(Boolean);
  const uniqueTerminals = new Set(ticks.map(t => t.terminal_id).filter(Boolean));
  const activelyPolled = ticks.filter(t => ageSeconds(t.received_at) < 2.0);
  const connectedWorkers = terminals.filter(t => (workerStates[t.id] || t.worker || {}).connected).length;
  const issues = activeSlotIds
    .map(slotId => {
      const slotNumber = Number(slotId.split('-').pop());
      return liveSlotIssueDetails(liveStreams[slotId] || {}, liveTicks[slotId], slotNumber);
    })
    .filter(Boolean);

  el.classList.remove('ok', 'warn');
  if (activeSlotIds.length === 3 && activelyPolled.length === 3 && uniqueTerminals.size === 3) {
    el.classList.add('ok');
    setHtmlIfChanged(el, `<strong>✓ Simultaneidade confirmada:</strong> 3 fluxos recentes, vindos de 3 terminais e PIDs independentes. Workers conectados: ${connectedWorkers}.`);
  } else if (activeSlotIds.length || stoppedCount) {
    el.classList.add('warn');
    const issueText = issues.map(issue => (
      `<strong>Fluxo ${issue.slotNumber}</strong> — ${escapeHtml(issue.terminalLabel)} · ${escapeHtml(issue.symbolName)}: ${escapeHtml(issue.detail)}`
    )).join('; ');
    const activeSummary = activeSlotIds.length
      ? `${activelyPolled.length}/${activeSlotIds.length} fluxo(s) ativo(s) com leitura recente`
      : 'nenhum fluxo ativo';
    const stoppedSummary = stoppedCount ? ` · ${stoppedCount} fluxo(s) parado(s)` : '';
    const heading = activeSlotIds.length ? 'Teste em andamento:' : 'Teste parado:';
    setHtmlIfChanged(el, `<strong>${heading}</strong> ${activeSummary}${stoppedSummary} · ${connectedWorkers} worker(s) conectado(s).${issueText ? `<br>Atenção: ${issueText}.` : ''}`);
  } else {
    setTextIfChanged(el, `Configure os painéis para comprovar as conexões simultâneas. Workers conectados: ${connectedWorkers}.`);
  }
}

async function loadRuntimeLimits() {
  if (!bridge?.getRuntimeLimits) return;
  const res = parseResponse(await bridge.getRuntimeLimits());
  if (res.ok && res.data) {
    runtimeLimits = { ...runtimeLimits, ...res.data };
    const limit = activeTerminalLimit();
    const description = document.getElementById('activeTerminalLimitDescription');
    if (description && limit) {
      description.textContent = `Cadastre quantos terminais precisar. Até ${limit} podem ficar abertos/conectados simultaneamente. Abrir um MT5 também inicia sua leitura.`;
    }
    const hint = document.getElementById('activeTerminalLimitHint');
    if (hint && limit) {
      hint.textContent = `Primeira execução: abra o terminal criado e faça login manualmente no próprio MT5. A combinação Corretora + Conta não pode se repetir. Os cadastros são ilimitados; a política atual permite ${limit} MT5 simultâneos.`;
    }
  }
}

async function loadTerminals() {
  if (!bridge) return;
  await loadRuntimeLimits();
  const res = parseResponse(await bridge.getTerminals());
  if (!res.ok) return toast(res.message, true);
  renderTerminals(res.data);
}

async function reloadTerminals() {
  const button = document.getElementById('btnReloadTerminals');
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = 'Atualizando...';
  try {
    await loadTerminals();
  } finally {
    button.textContent = 'Atualizar';
    button.disabled = false;
  }
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
  const worker = workerStates[terminal.id] || terminal.worker || {};
  if (terminal.running || worker.alive) {
    return toast('Feche o MT5 e pare a leitura antes de editar este terminal.', true);
  }
  document.getElementById('editTerminalId').value = terminal.id;
  document.getElementById('editTerminalLabel').value = terminal.label || '';
  document.getElementById('editBrokerName').value = terminal.broker_name || '';
  document.getElementById('editAccountLogin').value = terminal.account_login || '';
  document.getElementById('editTerminalDetails').innerHTML = `
    <strong>Instância controlada</strong>
    <span>${escapeHtml(terminal.instance_dir || '')}</span>
    <span>Conta detectada: ${escapeHtml(worker.account_login || 'ainda não detectada')}</span>
    <span>Servidor detectado: ${escapeHtml(worker.server || 'ainda não detectado')}</span>
  `;
  setEditTerminalMessage();
  document.getElementById('editTerminalModal').classList.remove('hidden');
  document.getElementById('editTerminalLabel').focus();
}

function closeEditTerminal() {
  document.getElementById('editTerminalModal').classList.add('hidden');
  setEditTerminalMessage();
}

function setEditTerminalMessage(message = '') {
  const element = document.getElementById('editTerminalMessage');
  element.textContent = message;
  element.classList.toggle('hidden', !message);
}

async function saveTerminalEdit() {
  const id = document.getElementById('editTerminalId').value;
  const label = document.getElementById('editTerminalLabel').value;
  const broker = document.getElementById('editBrokerName').value;
  const login = document.getElementById('editAccountLogin').value;
  const button = document.getElementById('btnSaveTerminalEdit');
  button.disabled = true;
  button.textContent = 'Salvando...';
  setEditTerminalMessage();
  try {
    const res = parseResponse(await bridge.updateTerminal(id, label, broker, login));
    toast(res.message, !res.ok);
    if (res.ok) {
      closeEditTerminal();
      await loadTerminals();
      await loadWorkerStates();
    } else {
      setEditTerminalMessage(res.message || 'Não foi possível salvar as alterações.');
      await loadWorkerStates();
      await loadTerminals();
    }
  } catch (error) {
    const message = error?.message || 'Falha inesperada ao salvar as alterações.';
    setEditTerminalMessage(message);
    toast(message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Salvar alterações';
  }
}

function openDeleteTerminal(id) {
  const terminal = terminals.find(t => t.id === id);
  if (!terminal) return toast('Terminal não encontrado.', true);
  const worker = workerStates[terminal.id] || terminal.worker || {};
  if (terminal.running || worker.alive) {
    return toast('Feche o MT5 e pare a leitura antes de excluir esta instância.', true);
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

function setInstanceResolutionMessage(message = '') {
  const element = document.getElementById('instanceResolutionMessage');
  element.textContent = message;
  element.classList.toggle('hidden', !message);
}

function openInstanceResolution(id) {
  const terminal = terminals.find(item => item.id === id);
  if (!terminal) return toast('Terminal não encontrado.', true);
  const worker = workerStates[terminal.id] || terminal.worker || {};
  if (terminal.running || worker.alive) {
    return toast('Feche o MT5 e pare a leitura antes de resolver esta instância.', true);
  }
  if (terminalInstanceReady(terminal)) {
    return toast('A instância local está pronta. Use Excluir para removê-la.', true);
  }

  document.getElementById('instanceResolutionTerminalId').value = terminal.id;
  document.getElementById('instanceResolutionDetails').innerHTML = `
    <strong>${escapeHtml(terminal.label || terminal.id)}</strong>
    <span>Corretora: ${escapeHtml(terminal.broker_name || '—')}</span>
    <span>Conta: ${escapeHtml(terminal.account_login || '—')}</span>
    <span>Diagnóstico: ${escapeHtml(terminal.instance_status?.message || 'Instância local indisponível.')}</span>
    <span>Caminho esperado: ${escapeHtml(terminal.instance_status?.path || terminal.instance_dir || '—')}</span>
  `;
  setInstanceResolutionMessage();
  document.getElementById('instanceResolutionModal').classList.remove('hidden');
}

function closeInstanceResolution() {
  document.getElementById('instanceResolutionModal').classList.add('hidden');
  setInstanceResolutionMessage();
}

function setInstanceResolutionBusy(busy, activeAction = '') {
  const recreateButton = document.getElementById('btnRecreateTerminalInstance');
  const removeButton = document.getElementById('btnRemoveMissingTerminal');
  recreateButton.disabled = busy;
  removeButton.disabled = busy;
  recreateButton.textContent = busy && activeAction === 'recreate' ? 'Recriando...' : 'Recriar instância';
  removeButton.textContent = busy && activeAction === 'remove' ? 'Removendo...' : 'Remover cadastro';
}

async function recreateTerminalInstance() {
  const id = document.getElementById('instanceResolutionTerminalId').value;
  setInstanceResolutionBusy(true, 'recreate');
  setInstanceResolutionMessage();
  try {
    const res = parseResponse(await bridge.recreateTerminalInstance(id));
    toast(res.message, !res.ok);
    if (res.ok) {
      closeInstanceResolution();
      await loadTerminals();
      await loadWorkerStates();
    } else {
      setInstanceResolutionMessage(res.message);
    }
  } catch (error) {
    const message = error?.message || 'Falha inesperada ao recriar a instância.';
    setInstanceResolutionMessage(message);
    toast(message, true);
  } finally {
    setInstanceResolutionBusy(false);
  }
}

async function removeMissingTerminal() {
  const id = document.getElementById('instanceResolutionTerminalId').value;
  setInstanceResolutionBusy(true, 'remove');
  setInstanceResolutionMessage();
  try {
    const res = parseResponse(await bridge.removeMissingTerminal(id));
    toast(res.message, !res.ok);
    if (res.ok) {
      delete workerStates[id];
      delete snapshots[id];
      selectedTerminalIds.delete(id);
      persistSelectedTerminals();
      Object.keys(liveTicks).forEach(slotId => {
        if (liveTicks[slotId]?.terminal_id === id) delete liveTicks[slotId];
      });
      closeInstanceResolution();
      await loadTerminals();
      await loadWorkerStates();
      await loadLiveStreams();
    } else {
      setInstanceResolutionMessage(res.message);
    }
  } catch (error) {
    const message = error?.message || 'Falha inesperada ao remover o cadastro.';
    setInstanceResolutionMessage(message);
    toast(message, true);
  } finally {
    setInstanceResolutionBusy(false);
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

function closeBatchSummary(total, failures) {
  const closed = total - failures;
  if (failures) {
    return `${closed} de ${total} terminal(is) fechado(s); ${failures} falha(s).`;
  }
  return `${closed} terminal(is) fechado(s).`;
}

async function closeSelectedTerminals() {
  const ids = selectedTerminalList();
  if (!ids.length) return toast('Selecione pelo menos um terminal.', true);
  if (bulkCloseInProgress) return;

  const button = document.getElementById('btnCloseSelected');
  let failures = 0;
  bulkCloseInProgress = true;
  updateSelectionUi();
  try {
    for (let index = 0; index < ids.length; index++) {
      const terminalId = ids[index];
      if (button) button.textContent = `Fechando ${index + 1}/${ids.length}...`;
      const res = parseResponse(await bridge.stopTerminal(terminalId));
      if (res.ok) {
        Object.keys(liveTicks).forEach(slotId => {
          if (liveTicks[slotId]?.terminal_id === terminalId) delete liveTicks[slotId];
        });
      } else {
        failures += 1;
        const terminal = terminals.find(item => item.id === terminalId);
        toast(`${terminal?.label || terminalId}: ${res.message}`, true);
      }
      await loadWorkerStates();
      await loadTerminals();
      await loadLiveStreams();
    }
  } finally {
    bulkCloseInProgress = false;
    if (button) button.textContent = 'Fechar selecionados';
    updateSelectionUi();
  }
  toast(closeBatchSummary(ids.length, failures), failures > 0);
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
  if (res.ok) {
    document.getElementById(`liveTerminal${slotNumber}`).dataset.selectionTouched = '0';
    await loadLiveStreams();
  }
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
      closeInstanceResolution();
    }
  });
  document.querySelectorAll('.nav-item').forEach(btn => btn.addEventListener('click', () => switchView(btn.dataset.view)));
  document.getElementById('btnCreateTerminal').addEventListener('click', createTerminal);
  document.getElementById('btnSaveTerminalEdit').addEventListener('click', saveTerminalEdit);
  document.querySelectorAll('[data-close-edit]').forEach(el => el.addEventListener('click', closeEditTerminal));
  document.querySelectorAll('[data-close-delete]').forEach(el => el.addEventListener('click', closeDeleteTerminal));
  document.querySelectorAll('[data-close-instance-resolution]').forEach(el => el.addEventListener('click', closeInstanceResolution));
  document.getElementById('deleteTerminalConfirmation').addEventListener('input', updateDeleteConfirmationState);
  document.getElementById('btnConfirmDeleteTerminal').addEventListener('click', confirmDeleteTerminal);
  document.getElementById('btnRecreateTerminalInstance').addEventListener('click', recreateTerminalInstance);
  document.getElementById('btnRemoveMissingTerminal').addEventListener('click', removeMissingTerminal);
  document.getElementById('btnReloadTerminals').addEventListener('click', reloadTerminals);
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
  for (let i = 1; i <= 3; i++) {
    document.getElementById(`liveTerminal${i}`).addEventListener('change', event => {
      event.currentTarget.dataset.selectionTouched = '1';
      updateLiveSlotAction(i);
    });
    document.getElementById(`liveSymbol${i}`).addEventListener('change', () => updateLiveSlotAction(i));
  }

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
