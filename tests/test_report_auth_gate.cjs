const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const diagnostics = fs.readFileSync("web/diagnostics.js", "utf8");
const app = fs.readFileSync("web/app.js", "utf8").split("\ninitialize().catch(")[0];
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {
    hidden: false, value: "", textContent: "", dataset: {}, classList: {add() {}, remove() {}},
    files: [], disabled: false
  });
  return elements.get(id);
}
const sent = [];
const id = "00000000-0000-4000-8000-000000000000";
const context = vm.createContext({
  window: {}, document: {getElementById: el},
  navigator: {}, crypto: {randomUUID: () => id},
  performance: {now: () => 1}, Date, Set, Math, JSON, console,
  AbortController, setTimeout, clearTimeout,
  fetch: async (url) => {
    sent.push(url);
    const status = url.endsWith("bootstrap") ? 401 : 200;
    return {status, ok: status === 200,
      headers: {get: name => name === "X-Orderflow-Origin" ? "app"
        : name === "X-Orderflow-Request-Id" ? id : "application/json"},
      json: async () => status === 401 ? {error_code: "SESSION_EXPIRED"} : {status: "ok"}};
  }
});
vm.runInContext(diagnostics, context);
vm.runInContext(app, context);
(async () => {
  await vm.runInContext("api('bootstrap').catch(() => null)", context);
  assert.deepEqual(sent, ["/orderflow/api/bootstrap"],
    "pre-login 401 must not trigger a diagnostics POST");
  await vm.runInContext("api('login', {method:'POST'}).catch(() => null)", context);
  await new Promise(setImmediate);
  assert.deepEqual(sent, ["/orderflow/api/bootstrap", "/orderflow/api/login",
    "/orderflow/api/diagnostics"], "authenticated app response sends one bounded report");
  console.log("pre-login report gate and post-login delivery: ok");
})().catch(error => {console.error(error); process.exitCode = 1;});
