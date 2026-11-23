// Run with: node --test tests/web/cycle_clipboard.test.js
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(new URL('../../ioscoverage/web/static/cycles.js', `file://${__filename}`), 'utf8');

function workbench(clipboard, legacyResult = true) {
  const elements = new Map();
  const copied = [];
  const timers = new Map();
  let nextTimer = 0;
  let document;
  function element(tag) {
    return {
      tag, children: [], style: {}, handlers: {}, hidden: true,
      get classList() { return {contains: name => (this.className || '').split(' ').includes(name)}; },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      addEventListener(event, handler) { this.handlers[event] = handler; },
      focus() { document.activeElement = this; },
      select() { this.selected = true; },
      remove() { document.body.children.splice(document.body.children.indexOf(this), 1); }
    };
  }
  document = {
    createElement: element,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element('div'));
      return elements.get(id);
    },
    execCommand(command) {
      assert.equal(command, 'copy');
      const field = document.activeElement;
      assert.equal(field.tag, 'textarea');
      assert.equal(field.selected, true);
      copied.push(field.value);
      if (legacyResult instanceof Error) throw legacyResult;
      return legacyResult;
    }
  };
  document.body = element('body');
  const focused = element('button');
  document.activeElement = focused;
  const context = vm.createContext({
    document, navigator: {clipboard}, fetch: () => new Promise(() => {}),
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }
  });
  vm.runInContext(source, context);
  return {context, document, focused, copied, timers, status: document.getElementById('copy-status')};
}

test('LAN HTTP without Clipboard API copies through both actual button handlers', async () => {
  const page = workbench(undefined);
  vm.runInContext("renderCycles([{id:'cycle-list', build_count:0, run_count:0}])", page.context);
  const row = page.document.getElementById('cycle-rows').children[0];
  const copyButton = row.children.at(-1).children[1];
  await copyButton.handlers.click();
  assert.deepEqual(page.copied, ['cycle-list']);
  assert.match(page.status.textContent, /已复制 Cycle ID：cycle-list/);

  vm.runInContext("createdCycle = {id:'cycle-created'}", page.context);
  page.document.getElementById('copy-created-cycle').handlers.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(page.copied, ['cycle-list', 'cycle-created']);
  assert.match(page.status.textContent, /已复制 Cycle ID：cycle-created/);
  assert.equal(page.status.hidden, false);
  assert.equal(page.document.body.children.length, 0);
  assert.equal(page.document.activeElement, page.focused);
});

test('available Clipboard API copies the exact ID without legacy fallback', async () => {
  const writes = [];
  const page = workbench({writeText: async text => { writes.push(text); }});
  await vm.runInContext("copyCycleID('cycle-secure')", page.context);
  assert.deepEqual(writes, ['cycle-secure']);
  assert.deepEqual(page.copied, []);
  assert.match(page.status.textContent, /已复制/);
});

test('rejected Clipboard API falls back to legacy copy', async () => {
  const page = workbench({writeText: async () => { throw new Error('denied'); }});
  await vm.runInContext("copyCycleID('cycle-denied')", page.context);
  assert.deepEqual(page.copied, ['cycle-denied']);
  assert.match(page.status.textContent, /已复制/);
});

test('repeated copy restarts the success notice duration', async () => {
  const page = workbench(undefined);
  await vm.runInContext("copyCycleID('cycle-first')", page.context);
  const firstTimer = [...page.timers.keys()][0];
  await vm.runInContext("copyCycleID('cycle-second')", page.context);
  assert.equal(page.timers.has(firstTimer), false);
  assert.equal(page.timers.size, 1);
  const timer = [...page.timers.values()][0];
  assert.equal(timer.delay, 3000);
  assert.equal(page.status.hidden, false);
  assert.match(page.status.textContent, /cycle-second/);
  timer.callback();
  assert.equal(page.status.hidden, true);
});

for (const result of [false, new Error('legacy copy denied')]) {
  test(`failed copy reports manual fallback and cleans up (${String(result)})`, async () => {
    const page = workbench(undefined, result);
    await vm.runInContext("copyCycleID('cycle-manual')", page.context);
    assert.match(page.status.textContent, /复制失败，请手动复制 Cycle ID：cycle-manual/);
    assert.equal(page.status.className, 'status-message error');
    assert.equal(page.status.hidden, false);
    assert.equal([...page.timers.values()][0].delay, 8000);
    assert.equal(page.document.body.children.length, 0);
    assert.equal(page.document.activeElement, page.focused);
  });
}
