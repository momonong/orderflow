const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const elements = Object.fromEntries([
  'report', 'basic-button', 'basic-status', 'next-step', 'pdf-file',
  'upload-button', 'upload-status', 'selected-file', 'sample-body', 'report-section'
].map((id) => [id, {textContent: '', dataset: {}, disabled: false}]));
elements.report.value = '';
elements['pdf-file'].files = [];
elements['report-section'].classList = {add() {}};
const context = vm.createContext({
  document: {getElementById(id) {return elements[id];}},
  navigator: {userAgent: 'TestBrowser/1'},
  crypto: {randomUUID() {return '00000000-0000-4000-8000-000000000000';}},
  performance: {now() {return 0;}},
  Date, console
});
vm.runInContext(source, context);
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
  console.log('guided flow connection retry: ok');
})().catch((error) => {console.error(error); process.exitCode = 1;});
