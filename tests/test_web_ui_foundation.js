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

console.log('web UI foundation tests passed');
