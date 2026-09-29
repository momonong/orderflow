const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('web/test.html', 'utf8');
assert.match(html, /<section id="startup-section"[^>]*role="status">/);
assert.match(html, /<noscript>[^]*?JavaScript[^]*?<\/noscript>/);
assert.match(html, /<section id="login-section"[^>]* hidden>/);
assert.match(html, /<div id="workspace" hidden>/);
const source = fs.readFileSync('web/app.js', 'utf8').split('\ninitialize().catch(')[0];

function page() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      hidden: id === 'login-section' || id === 'workspace' || id === 'startup-retry',
      textContent: '', value: '', dataset: {}, listeners: {},
      addEventListener(name, handler) {this.listeners[name] = handler;}
    });
    return elements.get(id);
  };
  const context = vm.createContext({
    document: {getElementById: element, querySelector: () => ({})},
    window: {listeners: {}, addEventListener(name, handler) {this.listeners[name] = handler;}},
    getComputedStyle: () => ({backgroundColor: 'rgb(1, 2, 3)'}),
    navigator: {userAgent: 'TestBrowser/1'},
    crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000000'},
    performance: {now: () => 0}, Date, console, AbortController, setTimeout, clearTimeout
  });
  vm.runInContext(source, context);
  vm.runInContext('updateReport = () => {}; mark = () => {}; updateUploadChoice = () => {}; updateRealControls = () => {}; renderDocuments = () => {};', context);
  return {context, element};
}

(async () => {
  const delayed = page();
  delayed.element('ai-key').value = 'synthetic-browser-fill';
  delayed.context.api = () => new Promise((resolve) => { delayed.context.finishBootstrap = resolve; });
  const waiting = vm.runInContext('initialize()', delayed.context);
  assert.equal(delayed.element('startup-section').hidden, false);
  assert.equal(delayed.element('login-section').hidden, true);
  assert.equal(delayed.element('workspace').hidden, true);
  delayed.context.finishBootstrap({status: 200, data: {version: '0.3.0', max_pdf_bytes: 10, documents: [], jobs: [], ai_key_configured: false}});
  await waiting;
  assert.equal(delayed.element('startup-section').hidden, true);
  assert.equal(delayed.element('workspace').hidden, false);
  const keyField = delayed.element('ai-key');
  assert.equal(keyField.value, '', 'authorized reveal clears a browser-provided initial value');
  assert.equal(keyField.readOnly, true);
  keyField.value = 'synthetic-late-fill';
  delayed.context.window.listeners.pageshow({persisted: true});
  assert.equal(keyField.value, '', 'untouched BFCache return clears a restored value');
  keyField.listeners.focus();
  assert.equal(keyField.readOnly, false, 'focus unlocks normal typing and paste');
  keyField.value = 'opaqueA12!';
  keyField.listeners.input();
  delayed.context.window.listeners.pageshow({persisted: true});
  assert.equal(keyField.value, 'opaqueA12!', 'BFCache must keep intentionally entered text');
  vm.runInContext('showLogin()', delayed.context);
  keyField.value = 'synthetic-relogin-fill';
  delayed.context.api = async () => ({status: 200, data: {version: '0.3.0', max_pdf_bytes: 10, documents: [], jobs: [], ai_key_configured: false}});
  await vm.runInContext('loadBootstrap()', delayed.context);
  assert.equal(keyField.value, '', 'relogin reveal clears browser-provided key value');
  for (const invalid of ['', 'https://aistudio.google.com/apikey', 'has space', 'a\n']) {
    delayed.context.syntheticKey = invalid;
    assert.equal(typeof vm.runInContext('keyInputError(syntheticKey)', delayed.context), 'string');
  }
  delayed.context.syntheticKey = 'opaqueA12!';
  assert.equal(vm.runInContext('keyInputError(syntheticKey)', delayed.context), null);
  delayed.context.api = async () => {throw Error('invalid key must not call API');};
  keyField.value = 'https://aistudio.google.com/apikey';
  await vm.runInContext('saveKey()', delayed.context);
  assert.doesNotMatch(delayed.element('key-status').textContent, /aistudio\.google\.com/);

  const expired = page();
  vm.runInContext('api = async () => { showLogin("登入已過期"); throw {status: 401, code: "SESSION_EXPIRED"}; };', expired.context);
  await vm.runInContext('initialize()', expired.context);
  assert.equal(expired.element('startup-section').hidden, true);
  assert.equal(expired.element('login-section').hidden, false);
  assert.equal(expired.element('workspace').hidden, true);

  for (const error of [
    {code: 'REQUEST_TIMEOUT', unknown: true},
    {code: 'BAD_JSON_RESPONSE', status: 502, responseType: 'HTML'},
    {code: 'BAD_JSON_RESPONSE', status: 401, responseType: 'HTML'}
  ]) {
    const failed = page();
    failed.context.syntheticError = error;
    vm.runInContext('api = async () => { throw syntheticError; };', failed.context);
    await vm.runInContext('initialize()', failed.context);
    assert.equal(failed.element('startup-section').hidden, false);
    assert.equal(failed.element('login-section').hidden, true);
    assert.equal(failed.element('workspace').hidden, true);
    assert.equal(failed.element('startup-retry').hidden, false);
    assert.match(failed.element('startup-message').textContent, /重新檢查網站/);
    failed.context.api = async () => ({status: 200, data: {version: '0.3.0', max_pdf_bytes: 10, documents: [], jobs: [], ai_key_configured: false}});
    await failed.element('startup-retry').listeners.click();
    assert.equal(failed.element('startup-section').hidden, true);
    assert.equal(failed.element('workspace').hidden, false);
  }
  console.log('startup loading, auth, error and retry states: ok');
})().catch((error) => {console.error(error); process.exitCode = 1;});
