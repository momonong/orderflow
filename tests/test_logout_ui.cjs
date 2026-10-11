const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/manage.js', 'utf8').replace(/\nload\(\);\s*$/, '');
function page() {
  const elements = new Map();
  const make = () => ({textContent: '', innerHTML: '', value: '', dataset: {}, files: [],
    hidden: false, disabled: false, children: [], listeners: {},
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items; this.textContent = '';},
    addEventListener(name, handler) {this.listeners[name] = handler;},
    setAttribute(name, value) {this[name] = value;}});
  const el = id => {if (!elements.has(id)) elements.set(id, make()); return elements.get(id);};
  const context = vm.createContext({document: {getElementById: el, createElement: make},
    window: {confirm: () => true, addEventListener() {}},
    crypto: {randomUUID: () => '11111111-1111-4111-8111-111111111111'},
    fetch: async () => {throw Error('Unexpected network');},
    AbortController, setTimeout, clearTimeout, Date, console});
  vm.runInContext(source, context);
  return {context, el, run: code => vm.runInContext(code, context)};
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {resolve = yes; reject = no;});
  return {promise, resolve, reject};
}
const snapshot = {documents: [], jobs: [], record_sets: [
  {id: 'set-po', kind: 'purchase_order', document_id: 'doc-po', rows: [
    {id: 'po', orderNo: 'PO-retained', product: 'Part', code: 'A', qty: '2', unit: 'EA', deleted: false}]},
  {id: 'set-inv', kind: 'invoice', document_id: 'doc-inv', rows: [
    {id: 'inv', invoiceNo: 'INV-retained', product: 'Part', code: 'A', qty: '1', unit: 'EA', deleted: false}]},
]};
(async () => {
  const p = page();
  p.context.snapshot = snapshot;
  p.run('showWorkspace(snapshot)');
  assert.match(p.el('invoice-rows').innerHTML, /INV-retained/);
  const pending = deferred();
  const calls = [];
  p.context.api = path => {calls.push(path); return pending.promise;};
  const first = p.el('logout').listeners.click();
  await p.el('logout').listeners.click();
  assert.deepEqual(calls, ['logout'], 'double click sends one logout');
  assert.equal(p.el('logout').disabled, true);
  pending.resolve({status: 'signed_out'});
  await first;
  assert.equal(p.el('workspace').hidden, true);
  assert.equal(p.el('login').hidden, false);
  assert.equal(p.el('logout').disabled, false);

  const loggingIn = deferred();
  p.context.api = path => {
    calls.push(path);
    if (path === 'login') return loggingIn.promise;
    if (path === 'management/bootstrap') return Promise.resolve(snapshot);
    if (path === 'shared-documents') return Promise.resolve({documents: []});
    throw Error(path);
  };
  p.el('password').value = 'synthetic';
  const login = p.run('login({preventDefault() {}})');
  await p.run('login({preventDefault() {}})');
  assert.equal(calls.filter(path => path === 'login').length, 1);
  loggingIn.resolve({status: 'ok'});
  await login;
  assert.equal(p.el('workspace').hidden, false);
  assert.match(p.el('invoice-rows').innerHTML, /INV-retained/);
  assert.match(p.el('order-rows').innerHTML, /PO-retained/);

  const stale = deferred();
  p.context.api = path => path === 'management/bootstrap' ? stale.promise : Promise.resolve({status: 'signed_out'});
  const load = p.run('load()');
  await p.run('logout()');
  stale.resolve(snapshot);
  await load;
  assert.equal(p.el('workspace').hidden, true, 'pre-logout bootstrap cannot reopen private workspace');
  assert.equal(p.el('login').hidden, false);

  p.context.api = async () => {throw {code: 'REQUEST_TIMEOUT'};};
  await p.run('logout()');
  assert.equal(p.el('logout').disabled, false, 'uncertain logout remains retryable');
  assert.match(p.el('key-status').textContent, /登出未確認/);

  // A stale 401 from before reauthentication must not discard refreshed rows.
  const old = page();
  const delayed = deferred();
  old.context.fetch = () => delayed.promise;
  const request = old.run('api("management/bootstrap").catch(() => {})');
  old.context.snapshot = snapshot;
  old.run('showLogin(); showWorkspace(snapshot)');
  delayed.resolve({status: 401, ok: false,
    headers: {get: name => name.toLowerCase() === 'content-type' ? 'application/json' : 'app'},
    json: async () => ({error_code: 'AUTH_REQUIRED'})});
  await request;
  assert.equal(old.el('workspace').hidden, false);
  assert.match(old.el('invoice-rows').innerHTML, /INV-retained/);
  // The diagnostic page already disables buttons; programmatic/queued events
  // must respect that disabled state too.
  const diagnosticSource = fs.readFileSync('web/app.js', 'utf8');
  const diagnostic = page();
  diagnostic.context.$ = diagnostic.el;
  diagnostic.context.location = {reload() {}};
  diagnostic.context.setStatus = () => {};
  diagnostic.context.nextStep = () => {};
  diagnostic.run(diagnosticSource.slice(diagnosticSource.indexOf('async function login(event)'),
    diagnosticSource.indexOf('\nfunction setStatus(')));
  for (const action of ['login({preventDefault() {}})', 'logout()']) {
    const hold = deferred();
    let count = 0;
    diagnostic.context.api = () => {count++; return hold.promise;};
    const waiting = diagnostic.run(action);
    await diagnostic.run(action);
    assert.equal(count, 1);
    hold.resolve({});
    await waiting;
  }
  const integrationSource = fs.readFileSync('web/integration.js', 'utf8');
  const integration = page();
  integration.context.$ = integration.el;
  integration.context.refresh = async () => {};
  integration.context.status = () => {};
  integration.run('let loginPending = false;\n' + integrationSource.slice(
    integrationSource.indexOf('async function login(event)'), integrationSource.indexOf('\n$("login-form")')));
  const hold = deferred();
  let count = 0;
  integration.context.api = () => {count++; return hold.promise;};
  const waiting = integration.run('login({preventDefault() {}})');
  await integration.run('login({preventDefault() {}})');
  assert.equal(count, 1, 'integration login submit is guarded');
  hold.resolve({});
  await waiting;
  console.log('logout/login UI: duplicate clicks, retained Invoice/PO, stale responses, retry: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
