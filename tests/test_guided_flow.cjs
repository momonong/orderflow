const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/app.js', 'utf8').split('\ninitialize().catch(')[0];
const elements = Object.fromEntries([
  'report', 'basic-button', 'basic-status', 'next-step', 'pdf-file',
  'upload-button', 'upload-status', 'selected-file', 'sample-body', 'report-section', 'check-status'
].map((id) => [id, {textContent: '', dataset: {}, disabled: false}]));
elements.report.value = '';
elements['pdf-file'].files = [];
elements['report-section'].classList = {add() {}};
const context = vm.createContext({
  document: {getElementById(id) {return elements[id];}},
  navigator: {userAgent: 'TestBrowser/1'},
  crypto: {randomUUID() {return '00000000-0000-4000-8000-000000000000';}},
  performance: {now() {return 0;}},
  Date, console, AbortController, setTimeout, clearTimeout
});
vm.runInContext(source, context);
const originalApi = vm.runInContext('api', context);
vm.runInContext('renderRows = () => { $("sample-body").textContent = "中文 <測試>"; }; api = async () => ({data: {nonce: "00000000-0000-4000-8000-000000000000", rows: []}, status: 200});', context);

(async () => {
  await vm.runInContext('runBasic()', context);
  assert.match(elements.report.value, /同站 API：通過/);
  assert.match(elements['upload-status'].textContent, /連線已通過/);
  vm.runInContext('api = async () => { throw {code: "NETWORK_ERROR", unknown: true}; };', context);
  await vm.runInContext('runBasic()', context);
  assert.doesNotMatch(elements.report.value, /同站 API：通過/);
  assert.match(elements.report.value, /同站 API：結果不明/);
  assert.match(elements['upload-status'].textContent, /連線檢查尚未通過/);
  assert.equal(elements['upload-button'].disabled, true);
  context.fetch = async () => ({status: 502, ok: false,
    headers: {get: () => 'text/html; charset=utf-8'}, json: async () => {throw new SyntaxError('synthetic');}});
  const nonJson = await originalApi('key/check', {method: 'POST'}, 1000).then(() => null, (error) => error);
  assert.equal(nonJson.code, 'BAD_JSON_RESPONSE');
  assert.equal(nonJson.status, 502);
  assert.equal(nonJson.responseType, 'HTML');
  assert.equal(typeof nonJson.ms, 'number');
  const jobNonJson = await originalApi('jobs/00000000-0000-4000-8000-000000000000', {}, 1000)
    .then(() => null, error => error);
  assert.equal(jobNonJson.code, 'BAD_JSON_RESPONSE');
  assert.equal(jobNonJson.responseType, 'HTML');
  context.syntheticError = nonJson;
  vm.runInContext('aiKeyConfigured = true; updateRealControls = () => {}; api = async () => { throw syntheticError; };', context);
  await vm.runInContext('checkKey()', context);
  assert.match(elements.report.value, /Google 文字連線：結果不明/);
  assert.match(elements.report.value, /HTTP 502，回應類型 HTML，應用標記 MISSING/);
  assert.match(elements.report.value, /請求識別 00000000-0000-4000-8000-000000000000/);
  assert.match(elements.report.value, /BAD_JSON_RESPONSE/);
  context.fetch = async () => ({status: 502, ok: false,
    headers: {get: (name) => name === 'X-Orderflow-Origin' ? 'app'
      : name === 'X-Orderflow-Request-Id' ? '00000000-0000-4000-8000-000000000000'
      : 'application/json'},
    json: async () => ({error_code: 'AI_BAD_REQUEST', upstream_http_status: 400,
      upstream_reason: 'INVALID_ARGUMENT', message: 'private raw upstream message'})});
  const appJson = await originalApi('key/check', {method: 'POST'}, 1000).then(() => null, (error) => error);
  assert.equal(appJson.code, 'AI_BAD_REQUEST');
  assert.equal(appJson.appMarker, 'APP');
  assert.equal(appJson.upstreamStatus, 400);
  assert.equal(appJson.upstreamReason, 'INVALID_ARGUMENT');
  assert.doesNotMatch(JSON.stringify(appJson), /private raw upstream message/);
  context.syntheticError = appJson;
  await vm.runInContext('checkKey()', context);
  assert.match(elements.report.value, /上游 HTTP 400，上游分類 INVALID_ARGUMENT/);
  context.fetch = async () => {const error = new Error('private'); error.name = 'AbortError'; throw error;};
  const timeout = await originalApi('key/check', {method: 'POST'}, 1000).then(() => null, (error) => error);
  assert.equal(timeout.code, 'REQUEST_TIMEOUT');
  assert.equal(timeout.unknown, true);
  assert.equal(timeout.requestId, '00000000-0000-4000-8000-000000000000');
  assert.doesNotMatch(JSON.stringify(timeout), /private/);
  console.log('guided flow connection retry and safe HTTP diagnostic: ok');
})().catch((error) => {console.error(error); process.exitCode = 1;});
