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
  window: {confirm: () => false, listeners: {}, addEventListener(name, handler) {this.listeners[name] = handler;}},
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000000'},
  fetch: async () => { throw Error('Unexpected network request'); },
  AbortController, setTimeout, clearTimeout, Date, console,
});
el('ai-key').value = 'synthetic-browser-fill';
vm.runInContext(source, context);
assert.equal(el('ai-key').value, '');
assert.equal(el('ai-key').readOnly, true);
const docId = '11111111-1111-4111-8111-111111111111';
const jobId = '22222222-2222-4222-8222-222222222222';
const doc = {id: docId, size: 431, page_count: 1, created_ms: 1};
const job = {id: jobId, document_id: docId, state: 'done', scenario: 'real',
  result: [{description: '來源品項', quantity: 2}], created_ms: 2};
const snapshot = {documents: [doc], jobs: [job], drafts: [], ai_key_configured: true};
vm.runInContext('showWorkspace(snapshot)', Object.assign(context, {snapshot}));
assert.equal(el('ai-key').value, '');
el('ai-key').value = 'synthetic-late-fill';
context.window.listeners.pageshow({persisted: true});
assert.equal(el('ai-key').value, '');
el('ai-key').listeners.focus();
assert.equal(el('ai-key').readOnly, false);
el('ai-key').value = 'opaqueA12!';
el('ai-key').listeners.input();
vm.runInContext('showWorkspace(snapshot)', context);
context.window.listeners.pageshow({persisted: true});
assert.equal(el('ai-key').value, 'opaqueA12!', 'normal entered key survives subsequent refresh');
vm.runInContext('showLogin()', context);
el('ai-key').value = 'synthetic-relogin-fill';
vm.runInContext('showWorkspace(snapshot)', context);
assert.equal(el('ai-key').value, '', 'relogin reveal clears browser-provided key value');
for (const invalid of ['', 'https://aistudio.google.com/apikey', 'has space', 'a\n']) {
  context.syntheticKey = invalid;
  assert.equal(typeof vm.runInContext('keyInputError(syntheticKey)', context), 'string');
}
context.syntheticKey = 'opaqueA12!';
assert.equal(vm.runInContext('keyInputError(syntheticKey)', context), null);
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
  el('ai-key').value = 'https://aistudio.google.com/apikey';
  await vm.runInContext('setKey()', context);
  assert.deepEqual(calls, [], 'invalid key must not call API');
  assert.doesNotMatch(el('key-status').textContent, /aistudio\.google\.com/);
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
  calls = [];
  context.api = async (path, options) => {
    calls.push({path, method: options.method, body: JSON.parse(options.body)});
    throw {code:'REQUEST_TIMEOUT'};
  };
  await vm.runInContext('recognize()', context);
  assert.equal(calls.length, 1, 'uncertain paid request is never automatically reposted');
  assert.match(el('job-status').textContent, /不會自動重送/);
  await vm.runInContext('recognize()', context);
  assert.equal(calls.length, 2, 'a second user confirmation is required');
  assert.equal(calls[0].body.request_key, calls[1].body.request_key,
    'explicit retry after unknown result retains the original paid-job idempotency key');
  console.log('management save, refresh, paid confirmation and uncertain retry: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
