const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'web', 'ui_foundation.js'), 'utf8');
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

const navItems = ['terminals', 'symbols', 'dashboard'].map(view => ({
  dataset: { view },
  classList: classList(),
}));
const views = ['terminals', 'symbols', 'dashboard'].map(view => ({
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
assert.equal(navItems[2].classList.contains('active'), true);
assert.equal(navItems[0].classList.contains('active'), false);
assert.equal(views[2].classList.contains('active'), true);
assert.equal(title.textContent, 'Dashboard');
assert.match(subtitle.textContent, /snapshots consolidados/);
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

console.log('web UI foundation tests passed');
