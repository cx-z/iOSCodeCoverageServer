// Run with: node --test tests/web/cycle_report.test.js
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const source = readFileSync(new URL('../../ioscoverage/web/static/report.js', `file://${__filename}`), 'utf8')
  .replace('__CYCLE_ID_JSON__', '"cycle"');

function snapshot(id = 'a'.repeat(64), version = '123') {
  const file = {file_id:'f'.repeat(64), repo_relative_path:'Guide.md', covered:0, uncovered:0, not_measurable:1, unknown:0};
  return {can_annotate:true, presentation_id:id, page_version:version,
    mr:{iid:'42', diff:{head_sha:'new-commit'}}, analysis:{status:'succeeded'},
    report:{generation:7, current_build_id:'old-build', updated_at:'2026-01-02T03:04:05Z',
      build_diff:{head_sha:'old-commit'}, summary:{changed_line_coverage_percent:99.83}},
    projection:{strict_match:false, files:[file], summary:{covered:0, uncovered:0, not_measurable:1, unknown:0, changed_line_coverage_percent:null}}};
}
function detail(s, text = 'latest documentation') {
  return {cycle_id:'cycle', presentation_id:s.presentation_id, page_version:s.page_version,
    file:s.projection.files[0], lines:[{new_line_number:3, state:'not_measurable', coverage_source:'none', source_text:text}]};
}
function page() {
  const elements = new Map(), calls = [], pending = [];
  function element() {
    return {children:[], handlers:{}, hidden:true, checked:false,
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      addEventListener(name, handler) { this.handlers[name] = handler; }};
  }
  const document = {createElement:element, getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  }};
  const context = vm.createContext({document, URLSearchParams,
    fetch(path) {
      calls.push(path);
      if (path === '/api/v1/auth/session') return Promise.reject(new Error('anonymous'));
      return new Promise((resolve, reject) => pending.push({path, resolve, reject}));
    }, setTimeout:()=>1, clearTimeout:()=>{}});
  vm.runInContext(source, context);
  const tick = () => new Promise(resolve => setImmediate(resolve));
  async function reply(payload, status = 200, index = 0) {
    pending.splice(index, 1)[0].resolve({ok:status === 200, status, json:async()=>payload});
    await tick();
  }
  return {context, calls, pending, reply, tick, el:id=>document.getElementById(id),
    run:script=>vm.runInContext(script, context)};
}

test('latest MR counts and source are separate from formal build percentage', async () => {
  const p = page(); await p.tick(); const s = snapshot(); await p.reply(s);
  assert.equal(p.calls.at(-1), '/api/v1/cycles/cycle/mr-presentation');
  assert.equal(p.el('not-measurable').textContent, 1);
  assert.equal(p.el('percent').textContent, '—');
  assert.match(p.el('report-meta').textContent, /new-comm/);
  assert.match(p.el('build-report-meta').textContent, /old-build.*99.83%/);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  assert.match(p.pending[0].path, /page_version=123&presentation_id=a{64}&file_id=f{64}/);
  await p.reply(detail(s)); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'latest documentation');
  p.el('only-uncovered').checked = true;
  p.el('only-uncovered').handlers.change();
  assert.match(p.el('file-list').children[0].textContent, /没有符合/);
});

test('new presentation at the same generation invalidates old rows and late detail', async () => {
  const p = page(); await p.tick(); const a = snapshot(); await p.reply(a);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  const refreshing = p.run('refresh()');
  await p.reply(snapshot('b'.repeat(64), '124'), 200, 1); await refreshing;
  await p.reply(detail(a, 'old text')); await loading;
  assert.match(p.el('detail').children[0].textContent, /选择一个文件/);
  assert.equal(p.el('content').hidden, false);
});

test('unchanged polling allows a pending bound detail to finish', async () => {
  const p = page(); await p.tick(); const s = snapshot(); await p.reply(s);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  const refreshing = p.run('refresh()'); await p.reply(snapshot(), 200, 1); await refreshing;
  await p.reply(detail(s)); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'latest documentation');
});

test('file clicks cannot overwrite a newer selection', async () => {
  const p = page(); await p.tick(); const s = snapshot(); await p.reply(s);
  const first = p.run("loadFile('" + 'f'.repeat(64) + "')");
  const second = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply(detail(s, 'second'), 200, 1); await second;
  await p.reply(detail(s, 'first')); await first;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'second');
});

test('expired detail refreshes presentation instead of using old report rows', async () => {
  const p = page(); await p.tick(); const s = snapshot(); await p.reply(s);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply({error:'presentation_expired'}, 409);
  assert.equal(p.pending[0].path, '/api/v1/cycles/cycle/mr-presentation');
  const next = snapshot('b'.repeat(64), '124');
  await p.reply(next);
  assert.match(p.pending[0].path, /page_version=124&presentation_id=b{64}/);
  await p.reply(detail(next)); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'latest documentation');
});

test('unverified latest MR falls back to separately labeled formal report', async () => {
  const p = page(); await p.tick(); await p.reply(snapshot());
  const refreshing = p.run('refresh()');
  await p.reply({can_annotate:false, projection:null, status:'page_version_unverified'});
  assert.equal(p.pending[0].path, '/api/v1/cycles/cycle/report');
  const s = snapshot();
  await p.reply({...s.report, files:s.projection.files}); await refreshing;
  assert.equal(p.el('content').hidden, false);
  assert.match(p.el('report-notice').textContent, /MR 正在更新.*非最新 MR diff/);
  assert.equal(p.el('diff-title').textContent, '构建变更文件与新侧行');
});

test('non-MR legacy cycle retains formal summary and report file API', async () => {
  const p = page(); await p.tick(); const s = snapshot(null, null); s.mr = null;
  s.projection.strict_match = true; s.projection.summary.changed_line_coverage_percent = 99.83;
  await p.reply(s); assert.equal(p.el('percent').textContent, '99.83%');
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  assert.match(p.pending[0].path, /\/report\/files\/f{64}$/);
  await p.reply(detail(s)); await loading;
});

for (const unavailable of ['closed', 'gitlab-error', 'network-error']) {
  test(`${unavailable} still permits formal report source browsing and later MR recovery`, async () => {
    const p = page(); await p.tick(); const s = snapshot();
    if (unavailable === 'closed') {
      await p.reply({can_annotate:false, projection:null, status:'mr_not_open'});
    } else if (unavailable === 'network-error') {
      p.pending.shift().reject(new TypeError('Failed to fetch'));
      await p.tick();
    } else {
      await p.reply({error:'gitlab_unavailable'}, 502);
    }
    assert.equal(p.pending[0].path, '/api/v1/cycles/cycle/report');
    await p.reply({...s.report, orchestration_input_sha256:'old-input', files:s.projection.files});
    assert.equal(p.el('content').hidden, false);
    assert.equal(p.el('page-error').hidden, true);
    assert.match(p.el('report-notice').textContent, /正式构建报告，非最新 MR diff/);
    if (unavailable === 'closed') assert.match(p.el('report-notice').textContent, /已关闭或已合并/);
    const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
    assert.match(p.pending[0].path, /\/report\/files\/f{64}$/);
    await p.reply(detail(s, 'formal build source')); await loading;
    assert.equal(p.el('detail').children[0].children[3].textContent, 'formal build source');
    const refreshing = p.run('refresh()'); await p.reply(s); await refreshing;
    assert.match(p.el('diff-title').textContent, /最新 MR/);
    assert.equal(p.el('report-notice').textContent, '');
    assert.match(p.el('detail').children[0].textContent, /选择一个文件/);
  });
}

test('missing or corrupt formal report is never fabricated as fallback', async () => {
  const p = page(); await p.tick();
  await p.reply({can_annotate:false, projection:null, status:'mr_not_open'});
  await p.reply({error:'report_not_available'}, 409);
  assert.equal(p.el('content').hidden, true);
  assert.equal(p.el('page-error').hidden, false);
  assert.match(p.el('page-error').textContent, /report_not_available/);
});

test('authorization failure does not attempt report fallback', async () => {
  const p = page(); await p.tick(); await p.reply({error:'unauthorized'}, 401);
  assert.equal(p.pending.length, 0);
  assert.equal(p.el('content').hidden, true);
});

function twoFiles() {
  const s = snapshot();
  s.projection.files.push({...s.projection.files[0], file_id:'e'.repeat(64), repo_relative_path:'Other.md'});
  return s;
}

test('same-ID expiry restores requested B after displaying A, even during an existing refresh', async () => {
  const p = page(); await p.tick(); const s = twoFiles(); await p.reply(s);
  const first = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply(detail(s, 'file A')); await first;
  const loading = p.run("loadFile('" + 'e'.repeat(64) + "')");
  const refreshing = p.run('refresh()');
  await p.reply({error:'presentation_expired'}, 409);
  assert.equal(p.pending.length, 1); // Recovery joins the existing refresh.
  await p.reply(twoFiles()); await refreshing;
  assert.match(p.pending[0].path, /file_id=e{64}/);
  await p.reply({...detail(s, 'file B'), file:s.projection.files[1]}); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'file B');
  assert.match(p.el('file-list').children[1].className, /active/);
});

test('expiry retries only once and exposes a second failure', async () => {
  const p = page(); await p.tick(); const s = snapshot(); await p.reply(s);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply({error:'presentation_expired'}, 409); await p.reply(snapshot());
  await p.reply({error:'presentation_expired'}, 409); await loading;
  assert.equal(p.pending.length, 0);
  assert.match(p.el('detail').children[0].textContent, /presentation_expired/);
});

test('expiry recovery never overrides a newer user selection', async () => {
  const p = page(); await p.tick(); const s = twoFiles(); await p.reply(s);
  const loading = p.run("loadFile('" + 'e'.repeat(64) + "')");
  await p.reply({error:'presentation_expired'}, 409);
  const newer = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply(detail(s, 'newer click'), 200, 1); await newer;
  await p.reply(twoFiles()); await loading;
  assert.equal(p.pending.length, 0);
  assert.equal(p.el('detail').children[0].children[3].textContent, 'newer click');
});

test('analysis timestamps remain separate from immutable report publication time', async () => {
  const p = page(); await p.tick(); await p.reply(snapshot());
  const before = p.el('updated-at').textContent;
  p.run("setStatus({status:'running',requested_at:'2026-02-01T01:00:00Z',started_at:'2026-02-01T01:01:00Z'})");
  assert.match(p.el('analysis-times').textContent, /请求.*开始/);
  assert.equal(p.el('updated-at').textContent, before);
});

function diffOnly() {
  const s = snapshot();
  s.status = 'diff_only'; s.report = null; s.build_count = 0;
  s.analysis = {status:'idle', completed_generation:0};
  s.notice = '尚无构建包；当前仅展示 MR diff，暂无覆盖率数据。';
  return s;
}

test('no-build Cycle displays verified diff and source without reading formal report', async () => {
  const p = page(); await p.tick(); const s = diffOnly(); await p.reply(s);
  assert.equal(p.el('content').hidden, false);
  assert.equal(p.el('percent').textContent, '—');
  assert.equal(p.el('status-text').textContent, '仅展示 diff');
  assert.equal(p.el('updated-at').textContent, '尚无构建报告');
  assert.match(p.el('report-notice').textContent, /尚无构建包/);
  assert.doesNotMatch(p.el('build-report-meta').textContent, /old-build|99.83/);
  assert.equal(p.calls.some(path => path.endsWith('/report')), false);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply(detail(s, '# Unbuilt doc')); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, '# Unbuilt doc');
});

test('diff-only polling and later first formal report replace old rows safely', async () => {
  const p = page(); await p.tick(); const s = diffOnly(); await p.reply(s);
  const polling = p.run('refresh()'); await p.reply(diffOnly()); await polling;
  assert.equal(p.el('content').hidden, false);
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  const refreshing = p.run('refresh()');
  const built = snapshot('b'.repeat(64));
  await p.reply(built, 200, 1); await refreshing;
  await p.reply(detail(s, 'obsolete unbuilt row')); await loading;
  assert.match(p.el('build-report-meta').textContent, /old-build.*99.83%/);
  assert.equal(p.el('report-notice').textContent, '');
  assert.match(p.el('detail').children[0].textContent, /选择一个文件/);
});

test('diff-only analysis failure does not claim verified Build evidence', async () => {
  const p = page(); await p.tick(); const s = diffOnly();
  s.analysis = {status:'failed'}; await p.reply(s);
  assert.match(p.el('analysis-note').textContent, /当前仅展示 diff/);
  assert.doesNotMatch(p.el('analysis-note').textContent, /已验证的构建证据/);
});

test('same-ID first Build registration updates metadata without clearing selected or pending detail', async () => {
  const p = page(); await p.tick(); const s = diffOnly(); await p.reply(s);
  const first = p.run("loadFile('" + 'f'.repeat(64) + "')");
  await p.reply(detail(s, 'selected source')); await first;
  const selectedRow = p.el('detail').children[0];
  const fileButton = p.el('file-list').children[0];
  const loading = p.run("loadFile('" + 'f'.repeat(64) + "')");
  const refreshing = p.run('refresh()');
  const registered = diffOnly();
  registered.build_count = 1;
  registered.notice = '尚无成功构建报告；当前仅展示 MR diff，暂无覆盖率数据。';
  registered.analysis.status = 'running';
  await p.reply(registered, 200, 1); await refreshing;
  assert.equal(p.el('build-report-meta').textContent, '尚无成功的正式构建报告');
  assert.doesNotMatch(p.el('build-report-meta').textContent, /尚无构建包/);
  assert.equal(p.el('status-text').textContent, '分析中');
  assert.equal(p.el('report-notice').textContent, registered.notice);
  assert.equal(p.el('detail').children[0], selectedRow);
  assert.equal(p.el('file-list').children[0], fileButton);
  assert.match(fileButton.className, /active/);
  await p.reply(detail(registered, 'pending source')); await loading;
  assert.equal(p.el('detail').children[0].children[3].textContent, 'pending source');
});
