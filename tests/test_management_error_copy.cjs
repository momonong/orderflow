const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/index.html', 'utf8');
for (const id of ['job-error-tools', 'job-error-details', 'copy-job-error', 'job-error-copy-status']) {
  assert.match(html, new RegExp(`id="${id}"`));
}
const source = fs.readFileSync('web/manage.js', 'utf8').replace(/\nload\(\);\s*$/, '');
const elements = new Map();
function makeElement() {
  return {textContent: '', value: '', dataset: {}, files: [], hidden: false, disabled: false,
    children: [], listeners: {}, focused: false, selected: false,
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items; this.textContent = '';},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    setAttribute(name, value) {this[name] = value;},
    focus() {this.focused = true;}, select() {this.selected = true;}};
}
function el(id) {if (!elements.has(id)) elements.set(id, makeElement()); return elements.get(id);}
let copied = null;
const context = vm.createContext({
  document: {getElementById: el, createElement: makeElement},
  window: {addEventListener() {}},
  navigator: {clipboard: {writeText: async text => {copied = text;}}},
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000000'},
  fetch: async () => {throw Error('unexpected network request');},
  AbortController, setTimeout, clearTimeout, Date, console,
});
vm.runInContext(source, context);
const docId = '11111111-1111-4111-8111-111111111111';
const firstId = '22222222-2222-4222-8222-222222222222';
const secondId = '33333333-3333-4333-8333-333333333333';
const doneId = '44444444-4444-4444-8444-444444444444';
const created = Date.UTC(2026, 8, 29, 8, 48, 25, 796);
const first = {id:firstId, document_id:docId, state:'unknown', error_code:'AI_TIMEOUT_UNKNOWN',
  created_ms:created, started_ms:created + 1, finished_ms:created + 25086,
  steps:{upstream_http_status:503, upstream_reason:'FAILED_PRECONDITION',
    transport_class:'TLS', raw_response:'PRIVATE_RESPONSE'},
  result:'PRIVATE_ITEMS', filename:'PRIVATE_FILENAME', key:'PRIVATE_KEY', cookie:'PRIVATE_COOKIE'};
const second = {id:secondId, document_id:docId, state:'failed', error_code:'PRIVATE_CODE',
  created_ms:null, started_ms:created + 25000, finished_ms:created + 1000,
  steps:{upstream_http_status:700, upstream_reason:'PRIVATE_REASON'}, result:'PRIVATE_RESULT'};
const done = {id:doneId, document_id:docId, state:'done', result:[], created_ms:created};
context.jobs = [first, second, done];
context.docId = docId;
vm.runInContext('state.jobs=jobs; state.selected=docId; selectJob(jobs[0].id)', context);
assert.equal(el('job-error-tools').hidden, false);
assert.match(el('job-status').textContent, /結果不明.*可能已送達 Google 並計費.*請勿連續按辨識/);
const summary = el('job-error-details').value;
assert.match(summary, new RegExp(firstId));
assert.match(summary, /created_utc: 2026-09-29T08:48:25\.796Z/);
assert.match(summary, /elapsed_ms: 25085/);
assert.match(summary, /upstream_http_status: 503/);
assert.match(summary, /upstream_reason: FAILED_PRECONDITION/);
assert.match(summary, /transport_class: TLS/);
assert.doesNotMatch(summary, /PRIVATE_|PDF|cookie|raw_response|result/);

(async () => {
  await vm.runInContext('copyDiagnosticReport()', context);
  assert.match(copied, new RegExp(firstId), 'one diagnostic copy includes the selected failed job ID');
  assert.match(copied, /error_code: AI_TIMEOUT_UNKNOWN/);
  assert.doesNotMatch(copied, /PRIVATE_/);

  await vm.runInContext('copyJobError()', context);
  assert.equal(copied, summary);
  assert.match(el('job-error-copy-status').textContent, /已複製/);

  vm.runInContext('selectJob(jobs[1].id)', context);
  const changed = el('job-error-details').value;
  assert.match(changed, new RegExp(secondId));
  assert.doesNotMatch(changed, new RegExp(firstId));
  assert.match(changed, /error_code: UNKNOWN/);
  assert.match(changed, /created_utc: unknown/);
  assert.match(changed, /elapsed_ms: unknown/);
  assert.match(changed, /upstream_http_status: unknown/);
  assert.match(changed, /upstream_reason: unknown/);
  assert.doesNotMatch(changed, /PRIVATE_/);
  assert.equal(el('job-error-copy-status').textContent, '');

  context.navigator.clipboard.writeText = async () => {throw Error('denied');};
  await vm.runInContext('copyJobError()', context);
  assert.equal(el('job-error-details').focused, true);
  assert.equal(el('job-error-details').selected, true);
  assert.match(el('job-error-copy-status').textContent, /手動複製/);

  vm.runInContext('selectJob(jobs[2].id)', context);
  assert.equal(el('job-error-tools').hidden, true);
  assert.equal(el('job-error-details').value, '');
  assert.equal(el('job-error-copy-status').textContent, '');
  copied = null;
  await vm.runInContext('copyJobError()', context);
  assert.equal(copied, null);

  context.navigator.clipboard.writeText = async text => {copied = text;};
  await vm.runInContext('copyDiagnosticReport()', context);
  assert.doesNotMatch(copied, new RegExp(firstId), 'a completed job omits stale failure details');

  vm.runInContext('selectJob(jobs[0].id); showLogin()', context);
  assert.equal(el('job-error-tools').hidden, true);
  assert.equal(el('job-error-details').value, '');
  console.log('management error summary whitelist, selection, UTC, copy and fallback: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
