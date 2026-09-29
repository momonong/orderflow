const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/manage.js', 'utf8').replace(/\nload\(\);\s*$/, '');
const html = fs.readFileSync('web/index.html', 'utf8');
assert.match(html, /\/orderflow\/test\//);
assert.match(html, /id="upload"/);
assert.match(html, /id="save-draft"/);
let calls = [];
const elements = new Map();
function makeElement() {
  return {textContent: '', value: '', dataset: {}, files: [], hidden: false, disabled: false,
    children: [], listeners: {}, append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items; this.textContent = '';},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    setAttribute(name, value) {this[name] = value;}};
}
function el(id) {if (!elements.has(id)) elements.set(id, makeElement()); return elements.get(id);}
const context = vm.createContext({
  document: {getElementById: el, createElement: makeElement},
  window: {confirm: () => false},
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000000'},
  fetch: async () => { throw Error('Unexpected network request'); },
  AbortController, setTimeout, clearTimeout, Date, console,
});
vm.runInContext(source, context);
const docId = '11111111-1111-4111-8111-111111111111';
const jobId = '22222222-2222-4222-8222-222222222222';
const doc = {id: docId, size: 431, page_count: 1, created_ms: 1};
const job = {id: jobId, document_id: docId, state: 'done', scenario: 'real',
  result: [{description: '來源品項', quantity: 2}], created_ms: 2};
const snapshot = {documents: [doc], jobs: [job], drafts: [], ai_key_configured: true};
vm.runInContext('showWorkspace(snapshot)', Object.assign(context, {snapshot}));
assert.equal(el('save-draft').disabled, false, 'unchanged AI rows still require an explicit Save');
assert.equal(el('draft-status').textContent.includes('未儲存'), true);
context.api = async (path, options) => {
  calls.push({path, method: options?.method});
  if (path === `management/drafts/${jobId}`) {
    const body = JSON.parse(options.body);
    return {id: '33333333-3333-4333-8333-333333333333', source_job_id: jobId,
      document_id: docId, rows: body.rows, revision: 1, updated_ms: 3};
  }
  throw Error(`unexpected ${path}`);
};
(async () => {
  await vm.runInContext('saveDraft()', context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'PUT');
  assert.equal(el('draft-status').textContent.includes('修訂 1'), true);
  vm.runInContext('markDirty()', context);
  assert.equal(el('draft-status').textContent.includes('未儲存'), true);
  const saved = vm.runInContext('state.drafts[0]', context);
  context.reloaded = {...snapshot, drafts: [saved]};
  vm.runInContext('showWorkspace(reloaded)', context);
  assert.equal(el('draft-status').textContent.includes('已儲存修訂 1'), true,
    'refresh restores the saved revision and loses local edits');
  calls = [];
  await vm.runInContext('recognize()', context);
  assert.deepEqual(calls, [], 'declined paid confirmation must not call API');
  assert.equal(el('recognize').disabled, false);
  context.window.confirm = () => true;
  context.api = async (path, options) => {calls.push({path, method: options?.method, body: options?.body}); return job;};
  await vm.runInContext('recognize()', context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, 'management/jobs');
  assert.equal(calls[0].method, 'POST');
  assert.equal(JSON.parse(calls[0].body).scenario, 'real');
  console.log('management save, refresh, and paid confirmation: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
