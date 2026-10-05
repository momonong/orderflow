const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("web/diagnostics.js", "utf8");
let next = 1;
const requests = [];
const context = vm.createContext({
  window: {},
  crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(next++).padStart(12, "0")}`},
  Date, Set, Math, JSON,
  fetch: async (path, options) => {
    requests.push({path, options});
    return {ok: true};
  }
});
vm.runInContext(source, context);
const reporter = context.window.OrderflowDiagnostics.create("/orderflow/api/");
assert.match(reporter.summary(), /診斷腳本版本：diag-20261005-01/);
assert.match(reporter.summary(), /操作開始（瀏覽器 UTC）：\d{4}-\d\d-\d\dT/);
assert.match(reporter.summary(), /複製時間（瀏覽器 UTC）：\d{4}-\d\d-\d\dT/);
const secret = "PRIVATE_PDF_AND_KEY_SENTINEL";
reporter.record("send", "management/jobs", {requestId: context.crypto.randomUUID(),
  code: secret, pdf_text: secret});
assert.doesNotMatch(reporter.summary(), /PRIVATE_PDF_AND_KEY_SENTINEL/);
(async () => {
  await reporter.flush(false);
  assert.equal(requests.length, 0, "no same-origin app response means no automatic report");
  reporter.record("http_received", "management/jobs", {
    requestId: context.crypto.randomUUID(), httpStatus: 502, responseType: "HTML",
    marker: "MISSING", durationMs: 25});
  await reporter.flush(true);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/orderflow/api/diagnostics");
  assert.equal(requests[0].options.credentials, "same-origin");
  const body = JSON.parse(requests[0].options.body);
  assert.equal(body.events.length, 2);
  assert.equal(body.events[1].route, "management_jobs");
  assert.doesNotMatch(requests[0].options.body, /PRIVATE_PDF_AND_KEY_SENTINEL/);
  await reporter.flush(true);
  assert.equal(requests.length, 1, "flush never recursively reports itself");
  context.fetch = async (path, options) => {
    requests.push({path, options});
    return {ok: false};
  };
  const failedReporter = context.window.OrderflowDiagnostics.create("/orderflow/api/");
  failedReporter.record("send", "jobs", {requestId: context.crypto.randomUUID()});
  await failedReporter.flush(true);
  failedReporter.record("request_unknown", "jobs", {code: "NETWORK_ERROR"});
  await failedReporter.flush(true);
  assert.equal(requests.length, 2, "failed reporting disables later automatic attempts");
  assert.match(failedReporter.summary(), /NETWORK_ERROR/,
    "manual report remains available after reporting fails");
  const sustained = context.window.OrderflowDiagnostics.create("/orderflow/api/");
  const submitId = context.crypto.randomUUID();
  const failedId = context.crypto.randomUUID();
  sustained.record("send", "management/jobs", {requestId: submitId});
  for (let index = 0; index < 80; index++) {
    const requestId = context.crypto.randomUUID();
    sustained.record("send", "management/jobs/" + requestId, {requestId});
    sustained.record("http_received", "management/jobs/" + requestId,
      {requestId, httpStatus: 200, responseType: "JSON"});
    sustained.record("marker_checked", "management/jobs/" + requestId, {requestId, marker: "APP"});
    sustained.record("json_parsed", "management/jobs/" + requestId, {requestId, httpStatus: 200});
  }
  sustained.record("http_received", "management/jobs/" + failedId,
    {requestId: failedId, httpStatus: 502, responseType: "HTML"});
  sustained.record("marker_checked", "management/jobs/" + failedId,
    {requestId: failedId, marker: "MISSING"});
  sustained.record("request_unknown", "management/jobs/" + failedId,
    {requestId: failedId, code: "NETWORK_ERROR"});
  for (let index = 0; index < 40; index++)
    sustained.record("send", "bootstrap", {requestId: context.crypto.randomUUID()});
  const summary = sustained.summary();
  assert.match(summary, new RegExp(submitId), "job submission stays visible after long polling");
  assert.match(summary, new RegExp(failedId), "HTML 502 and missing marker stay visible");
  assert.match(summary, /NETWORK_ERROR/);
  assert.doesNotMatch(summary, /route=management_job_get.*http_status=200/,
    "successful polling is omitted from copied report");
  console.log("bounded browser diagnostics and same-origin report: ok");
})().catch(error => {console.error(error); process.exitCode = 1;});
