const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');

async function check(clipboard) {
  const report = {value: '', focused: false, selected: false, focus() {this.focused = true;}, select() {this.selected = true;}};
  const status = {textContent: ''};
  const elements = {report, 'copy-status': status};
  const context = vm.createContext({
    document: {getElementById(id) {return elements[id];}},
    navigator: {userAgent: 'TestBrowser/1', clipboard},
    crypto: {randomUUID() {return '00000000-0000-4000-8000-000000000000';}},
    Date, console
  });
  vm.runInContext(source, context);
  vm.runInContext('currentDocument = {id: \"doc\", size: 400, page_count: 1, name: \"PRIVATE_PDF_FILENAME\", secret: \"API_SECRET\"}; currentJob = {id: \"job\", raw_response: \"RAW_AI_RESPONSE\"}; updateReport()', context);
  assert.match(report.value, /本機模擬/);
  assert.doesNotMatch(report.value, /PRIVATE_PDF_FILENAME|RAW_AI_RESPONSE|API_SECRET/);
  await vm.runInContext('copyReport()', context);
  return {report, status};
}

(async () => {
  let copied;
  const success = await check({writeText: async (text) => {copied = text;}});
  assert.match(copied, /報告複製：通過/);
  assert.equal(copied, success.report.value);
  assert.match(success.status.textContent, /已複製/);
  const failure = await check({writeText: async () => {throw Error('denied');}});
  assert.equal(failure.report.focused, true);
  assert.equal(failure.report.selected, true);
  assert.match(failure.report.value, /報告複製：失敗/);
  assert.match(failure.status.textContent, /手動複製/);
  console.log('report copy and manual fallback: ok');
})().catch((error) => {console.error(error); process.exitCode = 1;});
