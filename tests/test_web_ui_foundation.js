const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'ui_foundation.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '..', 'web', 'index.html'), 'utf8');
const context = { Intl };
vm.createContext(context);
vm.runInContext(source, context);

const ui = context.MarketHubUI;
assert.ok(ui);

const ordered = [
  { label: 'Terminal 10', broker_name: 'Z', account_login: '2' },
  { label: 'terminal 2', broker_name: 'Z', account_login: '1' },
  { label: 'Terminal 2', broker_name: 'A', account_login: '3' },
].sort(ui.compareTerminal);
assert.deepEqual(
  ordered.map(row => `${row.label}:${row.broker_name}:${row.account_login}`),
  ['Terminal 2:A:3', 'terminal 2:Z:1', 'Terminal 10:Z:2'],
);
const originalTerminals = [
  { id: 'z', label: 'Zulu' },
  { id: 'a', label: 'Alpha' },
  { id: 'm', label: 'Mike' },
];
const numbered = ui.numberTerminals(originalTerminals);
assert.deepEqual(
  numbered.map(row => `${row.id}:${row.display_number}`),
  ['a:1', 'm:2', 'z:3'],
);
assert.equal(ui.terminalDisplayNumber(numbered[0]), '01');
assert.equal(ui.terminalDisplayNumber({ display_number: 12 }), '12');
assert.equal(ui.terminalDisplayNumber({}), '—');
const renumbered = ui.numberTerminals(numbered.filter(row => row.id !== 'a'));
assert.deepEqual(
  renumbered.map(row => `${row.id}:${row.display_number}`),
  ['m:1', 'z:2'],
);
assert.equal(Object.hasOwn(originalTerminals[0], 'display_number'), false);

function classList() {
  const values = new Set();
  return {
    toggle(name, enabled) {
      if (enabled) values.add(name);
      else values.delete(name);
    },
    contains(name) { return values.has(name); },
  };
}

const navItems = ['dashboard', 'terminals', 'symbols', 'diagnostics'].map(view => ({
  dataset: { view },
  classList: classList(),
  attributes: {},
  setAttribute(name, value) { this.attributes[name] = value; },
  removeAttribute(name) { delete this.attributes[name]; },
}));
const views = ['dashboard', 'terminals', 'symbols', 'diagnostics'].map(view => ({
  id: `view-${view}`,
  classList: classList(),
}));
const title = { textContent: '' };
const subtitle = { textContent: '' };
const documentRef = {
  querySelectorAll(selector) {
    if (selector === '.nav-item') return navItems;
    if (selector === '.view') return views;
    return [];
  },
  getElementById(id) {
    if (id === 'viewTitle') return title;
    if (id === 'viewSubtitle') return subtitle;
    return null;
  },
};

assert.equal(ui.switchView(documentRef, 'dashboard'), true);
assert.equal(navItems[0].classList.contains('active'), true);
assert.equal(navItems[1].classList.contains('active'), false);
assert.equal(navItems[0].attributes['aria-current'], 'page');
assert.equal(views[0].classList.contains('active'), true);
assert.equal(title.textContent, 'Dashboard');
assert.match(subtitle.textContent, /processos MT5/);
assert.equal(ui.switchView(documentRef, 'diagnostics'), true);
assert.equal(navItems[3].classList.contains('active'), true);
assert.equal(navItems[0].attributes['aria-current'], undefined);
assert.equal(title.textContent, 'Diagnóstico');
assert.equal(ui.switchView(documentRef, 'unknown'), false);

const themeIcon = { textContent: '' };
const themeLabel = { textContent: '' };
const themeButton = {
  dataset: {},
  attributes: {},
  listeners: {},
  title: '',
  setAttribute(name, value) { this.attributes[name] = value; },
  querySelector(selector) {
    if (selector === '[data-theme-icon]') return themeIcon;
    if (selector === '[data-theme-label]') return themeLabel;
    return null;
  },
  addEventListener(name, listener) { this.listeners[name] = listener; },
};
const themeDocument = {
  documentElement: { dataset: {} },
  getElementById(id) { return id === 'themeToggle' ? themeButton : null; },
};
const storedValues = new Map();
const storage = {
  getItem(key) { return storedValues.get(key) || null; },
  setItem(key, value) { storedValues.set(key, value); },
};

assert.equal(ui.initializeTheme(themeDocument, storage), 'light');
assert.equal(themeDocument.documentElement.dataset.theme, 'light');
assert.equal(themeButton.attributes['aria-pressed'], 'false');
assert.equal(themeLabel.textContent, 'Tema escuro');
ui.bindThemeToggle(themeDocument, storage);
themeButton.listeners.click();
assert.equal(themeDocument.documentElement.dataset.theme, 'dark');
assert.equal(storage.getItem(ui.THEME_STORAGE_KEY), 'dark');
assert.equal(themeButton.attributes['aria-pressed'], 'true');
assert.equal(themeIcon.textContent, '☀');
assert.equal(themeLabel.textContent, 'Tema claro');
assert.equal(ui.initializeTheme(themeDocument, storage), 'dark');
assert.equal(ui.normalizeTheme('invalid'), 'light');

const health = ui.terminalHealthSummary([
  { id: 'one', running: true, process_state: 'open', instance_status: { state: 'ready' } },
  { id: 'two', running: true, process_state: 'open', instance_status: { state: 'ready' } },
  { id: 'three', running: false, process_state: 'closed', instance_status: { state: 'directory_missing' } },
  { id: 'four', running: false, process_state: 'closed', instance_status: { state: 'ready' } },
], {
  one: { alive: true, connected: true, state: 'connected' },
  two: { alive: true, connected: false, state: 'broker_disconnected' },
  three: { alive: false, connected: false, state: 'stopped' },
  four: { alive: false, connected: false, state: 'stopped' },
});
assert.deepEqual(
  JSON.parse(JSON.stringify(health)),
  { registered: 4, open: 2, workers: 2, connected: 1, attention: 2 },
);
assert.equal(
  ui.terminalNeedsAttention(
    { process_state: 'reconnecting', instance_status: { state: 'ready' } },
    { state: 'reconnecting' },
  ),
  false,
);

assert.match(html, /data-view="dashboard"[^>]*aria-current="page"/);
assert.match(html, /data-view="diagnostics"/);
const dashboardMarkup = html.split('id="view-dashboard"')[1].split('id="view-terminals"')[0];
const diagnosticsMarkup = html.split('id="view-diagnostics"')[1];
assert.match(dashboardMarkup, /id="dashboardTerminalHealth"/);
assert.doesNotMatch(dashboardMarkup, /id="liveProof"/);
assert.match(diagnosticsMarkup, /id="workerSummary"/);
assert.match(diagnosticsMarkup, /id="liveProof"/);
assert.match(diagnosticsMarkup, /id="snapshotTerminal"/);

console.log('web UI foundation tests passed');
