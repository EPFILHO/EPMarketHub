const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const context = {
  window: { addEventListener() {} },
  document: {},
  console,
  Date,
  Intl,
  setTimeout,
  clearTimeout,
};
vm.createContext(context);
vm.runInContext(source, context);

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

const terminalRows = [
  { id: 'one', running: true },
  { id: 'two', running: true },
  { id: 'three', running: true },
  { id: 'four', running: false },
];
const threeActiveWorkers = {
  one: { alive: true },
  two: { alive: true },
  three: { alive: true },
  four: { alive: false },
};
const fullCapacity = context.terminalBulkActionState(
  terminalRows,
  new Set(['one', 'two', 'three']),
  threeActiveWorkers,
  3,
);
assert.equal(fullCapacity.openDisabled, true);
assert.equal(fullCapacity.closeDisabled, false);

const twoTerminalCapacity = context.terminalBulkActionState(
  terminalRows,
  new Set(['one', 'two']),
  threeActiveWorkers,
  2,
);
assert.equal(twoTerminalCapacity.openDisabled, true);

const fourTerminalCapacity = context.terminalBulkActionState(
  terminalRows,
  new Set(['four']),
  threeActiveWorkers,
  4,
);
assert.equal(fourTerminalCapacity.openDisabled, false);

const waitingWorkerActions = context.terminalActionState(
  { id: 'one', running: true },
  { alive: true, connected: false },
  1,
  1,
  3,
);
assert.equal(waitingWorkerActions.readingBlocked, false);
assert.equal(waitingWorkerActions.readingLabel, 'Parar tentativa');
assert.equal(waitingWorkerActions.editBlocked, true);
assert.equal(waitingWorkerActions.deleteBlocked, true);

const fullWorkerCapacityActions = context.terminalActionState(
  { id: 'four', running: false },
  { alive: false, connected: false },
  1,
  2,
  2,
);
assert.equal(fullWorkerCapacityActions.openBlocked, true);
assert.equal(fullWorkerCapacityActions.readingBlocked, true);
assert.equal(context.workerLabel('stopped'), 'desconectado');
assert.equal(context.workerLabel(''), 'desconectado');
assert.equal(context.workerLabel('reopening_terminal'), 'reconectando');
assert.equal(
  context.terminalProcessLabel(
    { running: true, instance_status: { state: 'ready' } },
    { state: 'reopening_terminal' },
  ),
  'Reabrindo MT5',
);
assert.equal(
  context.terminalProcessLabel(
    { running: true, instance_status: { state: 'ready' } },
    { state: 'reconnecting' },
  ),
  'MT5 sem comunicação',
);
assert.equal(
  context.terminalProcessLabel(
    { running: false, instance_status: { state: 'directory_missing' } },
    { state: 'stopped' },
  ),
  'Instância ausente',
);
const missingInstanceActions = context.terminalActionState(
  { running: false, instance_status: { state: 'directory_missing' } },
  { alive: false },
  0,
  0,
  3,
);
assert.equal(missingInstanceActions.openBlocked, true);
assert.equal(missingInstanceActions.readingBlocked, true);
assert.equal(missingInstanceActions.editBlocked, true);
assert.equal(missingInstanceActions.deleteBlocked, false);
assert.equal(context.closeBatchSummary(3, 0), '3 terminal(is) fechado(s).');
assert.equal(
  context.closeBatchSummary(3, 1),
  '2 de 3 terminal(is) fechado(s); 1 falha(s).',
);
const progressiveCloseSource = context.closeSelectedTerminals.toString();
assert.match(progressiveCloseSource, /bridge\.stopTerminal\(terminalId\)/);
assert.doesNotMatch(progressiveCloseSource, /bridge\.closeSelectedTerminals/);

const closedBeyondCapacity = context.terminalBulkActionState(
  terminalRows,
  new Set(['four']),
  threeActiveWorkers,
  3,
);
assert.equal(closedBeyondCapacity.openDisabled, true);
assert.match(closedBeyondCapacity.openTitle, /limite de 3 MT5/);

const oneVacancy = context.terminalBulkActionState(
  terminalRows.map(row => row.id === 'three' ? { ...row, running: false } : row),
  new Set(['four']),
  { ...threeActiveWorkers, three: { alive: false } },
  3,
);
assert.equal(oneVacancy.openDisabled, false);

const configured = {
  config: {
    terminal_id: 'terminal-one',
    terminal_label: 'Terminal One',
    symbol: { id: 'eurusd', name: 'Euro/Dólar' },
  },
  status: { state: 'streaming' },
};

assert.deepEqual(
  plain(context.liveSlotActionState({}, 'terminal-one', 'eurusd')),
  { disabled: false, label: 'Iniciar', changed: false },
);
assert.deepEqual(
  plain(context.liveSlotActionState({}, '', 'eurusd')),
  { disabled: true, label: 'Iniciar', changed: false },
);
assert.deepEqual(
  plain(context.liveSlotActionState(configured, 'terminal-one', 'eurusd')),
  { disabled: true, label: 'Iniciar', changed: false },
);
assert.deepEqual(
  plain(context.liveSlotActionState(configured, 'terminal-one', 'usdjpy')),
  { disabled: false, label: 'Alterar', changed: true },
);
assert.deepEqual(
  plain(context.liveSlotActionState(configured, 'terminal-two', 'eurusd')),
  { disabled: false, label: 'Alterar', changed: true },
);
assert.deepEqual(
  plain(context.liveSlotActionState(
    { ...configured, status: { state: 'worker_stopped' } },
    'terminal-one',
    'eurusd',
  )),
  { disabled: true, label: 'Iniciar', changed: false },
);
assert.deepEqual(
  plain(context.liveSlotActionState(
    { ...configured, status: { state: 'worker_stopped' } },
    'terminal-two',
    'eurusd',
  )),
  { disabled: false, label: 'Alterar', changed: true },
);

const staleTick = {
  terminal_label: 'Terminal One',
  name: 'Euro/Dólar',
  received_at: new Date(Date.now() - 5000).toISOString(),
};
const issue = context.liveSlotIssueDetails(configured, staleTick, 2);
assert.equal(issue.slotNumber, 2);
assert.equal(issue.terminalLabel, 'Terminal One');
assert.equal(issue.symbolName, 'Euro/Dólar');
assert.match(issue.detail, /^leitura atrasada/);

const stopped = { ...configured, status: { state: 'worker_stopped' }, tick: staleTick };
assert.equal(context.liveSlotIssueDetails(stopped, staleTick, 1), null);
assert.equal(context.liveSlotVisibleTick(stopped, staleTick), null);
assert.deepEqual(
  plain(context.liveSlotVisibleTick(configured, staleTick)),
  plain(staleTick),
);

const connected = [
  { id: 'terminal-two' },
  { id: 'terminal-three' },
];
assert.equal(
  context.liveTerminalSelection(stopped, 'terminal-two', false, connected, 0),
  'terminal-one',
);
assert.equal(
  context.liveTerminalSelection(stopped, 'terminal-two', true, connected, 0),
  'terminal-two',
);

assert.doesNotMatch(source, /data-role="snapshot-button"/);
assert.doesNotMatch(source, /max_active_mt5\s*\|\|\s*3/);

console.log('web app state tests passed');
