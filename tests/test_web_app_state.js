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
  { disabled: false, label: 'Iniciar', changed: false },
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
assert.match(issue.detail, /^última leitura /);

console.log('web app state tests passed');
