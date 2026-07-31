(function exposeTerminalPresentation(root) {
  'use strict';

  function workerLabel(state) {
    const labels = {
      stopped: 'desconectado',
      starting: 'iniciando',
      stopping: 'desconectando',
      connected: 'conectado',
      waiting_login: 'aguardando login',
      authentication_failed: 'falha de autenticação',
      account_mismatch: 'conta divergente',
      broker_disconnected: 'corretora desconectada',
      reopening_terminal: 'reconectando',
      reconnecting: 'reconectando',
      configuration_error: 'configuração inválida',
      terminal_mismatch: 'terminal divergente',
      unresponsive: 'MT5 sem comunicação',
      attention_required: 'requer atenção',
      worker_start_failed: 'falha ao iniciar leitura',
      worker_crashed: 'worker interrompido',
      stop_failed: 'falha ao encerrar worker',
      error: 'erro',
    };
    return labels[state] || state || 'desconectado';
  }

  function workerBadgeClass(worker) {
    if (worker?.connected) return 'ok';
    if (['starting', 'stopping', 'waiting_login', 'reopening_terminal', 'reconnecting', 'broker_disconnected', 'attention_required'].includes(worker?.state)) return 'warn';
    if (['authentication_failed', 'account_mismatch', 'configuration_error', 'terminal_mismatch', 'unresponsive', 'worker_start_failed', 'worker_crashed', 'stop_failed', 'error'].includes(worker?.state)) return 'bad';
    return '';
  }

  function terminalInstanceState(terminal) {
    return terminal?.instance_status?.state || 'ready';
  }

  function terminalInstanceReady(terminal) {
    return terminalInstanceState(terminal) === 'ready';
  }

  function terminalProcessLabel(terminal) {
    const instanceState = terminalInstanceState(terminal);
    if (instanceState === 'directory_missing') return 'Instância ausente';
    if (instanceState === 'executable_missing') return 'Executável ausente';
    if (instanceState === 'invalid_path') return 'Caminho inválido';
    const processState = terminal?.process_state || (terminal?.running ? 'open' : 'closed');
    const labels = {
      closed: 'MT5 fechado',
      opening: 'Abrindo MT5',
      open: 'MT5 aberto',
      closing: 'Fechando MT5',
      reopening: 'Reabrindo MT5',
      launch_failed: 'Falha ao abrir MT5',
      close_failed: 'Falha ao fechar MT5',
      duplicate_process: 'Processos duplicados',
    };
    return labels[processState] || `MT5 ${terminal?.running ? 'aberto' : 'fechado'}`;
  }

  function terminalProcessBadgeClass(terminal) {
    if (!terminalInstanceReady(terminal)) return 'bad';
    const processState = terminal?.process_state || (terminal?.running ? 'open' : 'closed');
    if (['launch_failed', 'close_failed', 'duplicate_process'].includes(processState)) return 'bad';
    if (['opening', 'closing', 'reopening'].includes(processState)) return 'warn';
    return processState === 'open' ? 'ok' : '';
  }

  function terminalActionState(terminal, worker, openCount, activeWorkerCount, maxActive) {
    const workerAlive = Boolean(worker?.alive);
    const terminalEnabled = terminal?.enabled !== false;
    const instanceReady = terminalInstanceReady(terminal);
    const processState = terminal?.process_state || (terminal?.running ? 'open' : 'closed');
    const processBusy = ['opening', 'closing', 'reopening'].includes(processState);
    const processFaulted = processState === 'duplicate_process';
    const capacityUnavailable = !maxActive
      || openCount >= maxActive
      || activeWorkerCount >= maxActive;
    const openBlocked = !terminalEnabled || processBusy || !instanceReady || (!terminal.running && capacityUnavailable);
    const readingBlocked = workerAlive
      ? false
      : (!terminalEnabled || processBusy || processFaulted || !instanceReady || !terminal.running || !maxActive || activeWorkerCount >= maxActive);
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
      editBlocked: Boolean(processBusy || !instanceReady || terminal.running || workerAlive),
      deleteBlocked: Boolean(processBusy || terminal.running || workerAlive),
      closeBlocked: Boolean(processState === 'closing' || !terminal.running),
    };
  }

  root.MarketHubTerminalPresentation = Object.freeze({
    terminalActionState,
    terminalInstanceReady,
    terminalInstanceState,
    terminalProcessBadgeClass,
    terminalProcessLabel,
    workerBadgeClass,
    workerLabel,
  });
}(typeof globalThis !== 'undefined' ? globalThis : this));
