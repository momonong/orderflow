const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("web/app.js", "utf8").split("\ninitialize().catch(")[0];
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: "", value: "", dataset: {}, disabled: false,
    classList: {add() {}, remove() {}}
  });
  return elements.get(id);
}
const context = vm.createContext({
  window: {}, document: {getElementById: el},
  crypto: {randomUUID: () => "00000000-0000-4000-8000-000000000000"},
  navigator: {}, Date, console, Set, Math, JSON
});
vm.runInContext(source, context);
vm.runInContext(`let firstRender = true;
  renderRows = () => { if (firstRender) {firstRender = false; throw Error("synthetic DOM failure");} };
  updateRealControls = () => {}; reportReady = () => {};`, context);
context.job = {id: "11111111-1111-4111-8111-111111111111", mode: "mock",
  state: "done", steps: {ai: "pass", format: "pass"},
  result: [{description: "synthetic", quantity: 1}]};
vm.runInContext("showJob(job)", context);
assert.match(el("ai-status").textContent, /結果未能顯示/);
assert.match(el("report").value, /結果渲染：失敗/);
assert.doesNotMatch(el("report").value, /synthetic DOM failure/);
console.log("job render failure stays visible and content-free: ok");
