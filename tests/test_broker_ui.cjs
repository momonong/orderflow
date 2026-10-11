const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const MiB = 1024 * 1024;
const doc = {id: '11111111-1111-4111-8111-111111111111', size: MiB, created_ms: 1};
const complete = {id: '22222222-2222-4222-8222-222222222222', document_id: doc.id,
  mode: 'real', scenario: 'real', execution_mode: 'broker', state: 'done', created_ms: 2,
  steps: {ai: 'pass', format: 'pass'}, result: [{description: 'synthetic item', quantity: 1}],
  broker: {phase: 'complete', usage_unknown: true, response_truncated: false}};
const config = {mode: 'broker', ready: true, model: 'fixture-model', max_pdf_bytes: MiB, max_output_tokens: 1024};

function page(management) {
  const elements = new Map();
  const make = () => ({textContent: '', value: '', innerHTML: '', dataset: {}, files: [],
    hidden: false, disabled: false, children: [], listeners: {},
    classList: {add() {}, remove() {}},
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items; this.textContent = '';},
    addEventListener(name, handler) {this.listeners[name] = handler;},
    setAttribute(name, value) {this[name] = value;}});
  const el = id => {if (!elements.has(id)) elements.set(id, make()); return elements.get(id);};
  let uuidCount = 0;
  const context = vm.createContext({document: {getElementById: el, createElement: make, querySelector: () => ({})},
    window: {confirm: () => true, addEventListener() {}}, navigator: {},
    crypto: {randomUUID: () => {uuidCount++; return `00000000-0000-4000-8000-${String(uuidCount).padStart(12, '0')}`;}},
    performance: {now: () => 0}, getComputedStyle: () => ({backgroundColor: 'rgb(1, 2, 3)'}),
    fetch: async () => {throw Error('No network permitted in fixture');},
    AbortController, setTimeout, clearTimeout, Date, console});
  const filename = management ? 'web/manage.js' : 'web/app.js';
  const source = fs.readFileSync(filename, 'utf8');
  vm.runInContext(management ? source.replace(/\nload\(\);\s*$/, '') : source.split('\ninitialize().catch(')[0], context);
  return {context, el, run: code => vm.runInContext(code, context), uuidCount: () => uuidCount};
}
const snapshot = {documents: [doc], jobs: [], drafts: [], ai_key_configured: false, recognition: config};

(async () => {
  for (const htmlPath of ['web/index.html', 'web/test.html']) {
    const html = fs.readFileSync(htmlPath, 'utf8');
    assert.match(html, /id="direct-key-settings"/);
    assert.match(html, /id="recognition-status"/);
    assert.match(html, /id="refresh-job"/);
  }
  const p = page(true);
  p.context.snapshot = {...snapshot, documents: [{...doc}], recognition: {...config}};
  p.run('showWorkspace(snapshot)');
  assert.equal(p.el('recognize').disabled, false, 'Broker ready does not require personal key');
  assert.equal(p.el('direct-key-settings').hidden, true);
  assert.match(p.el('recognition-status').textContent, /網站提供的 AI 辨識，不必填入個人 API 金鑰.*1 MiB.*8 MiB/);
  assert.doesNotMatch(p.el('recognition-status').textContent, /Broker|direct|token/);
  let calls = [];
  p.context.api = async (path, options) => {calls.push({path, options}); return complete;};
  p.el('ai-key').value = 'synthetic-secret';
  await p.run('setKey()'); await p.run('clearKey()');
  assert.deepEqual(calls, [], 'Broker rejects queued or programmatic direct-key actions');
  await p.run('recognize()');
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, 'management/jobs');
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.scenario, 'real');
  assert.deepEqual(Object.keys(body).sort(), ['document_id', 'request_key', 'scenario']);
  assert.match(p.el('job-status').textContent, /辨識完成/);
  assert.doesNotMatch(p.el('job-status').textContent, /用量未知|Broker|direct|token/);
  assert.equal(p.el('save-draft').disabled, false, 'valid content can be used with unknown usage');
  const copied = [];
  p.context.navigator.clipboard = {writeText: async text => copied.push(text)};
  await p.run('copyDiagnosticReport()');
  assert.match(copied[0], /usage_unknown: true/);
  assert.doesNotMatch(copied[0], /synthetic item|synthetic-secret|endpoint|token/);
  const firstKey = body.request_key;
  calls = [];
  let confirmation = '';
  p.context.window.confirm = text => {confirmation = text; return false;};
  await p.run('recognize()');
  assert.deepEqual(calls, [], 'completed Broker retry still requires explicit paid confirmation');
  assert.match(confirmation, /新的辨識工作.*Google.*額度/);
  p.context.window.confirm = () => true;
  await p.run('recognize()');
  assert.equal(calls.length, 1);
  const repeat = JSON.parse(calls[0].options.body);
  assert.equal(repeat.new_attempt, true, 'only completed work allows an explicit new business attempt');
  assert.notEqual(repeat.request_key, firstKey);

  calls = [];
  p.run('state.documents[0].size += 1; refreshControls()');
  assert.equal(p.el('recognize').disabled, true);
  await p.run('recognize()');
  assert.deepEqual(calls, []);
  assert.match(p.el('job-status').textContent, /超過辨識上限 1 MiB/);
  p.run('state.recognition.max_pdf_bytes = 8 * 1024 * 1024; refreshControls()');
  assert.equal(p.el('recognize').disabled, false, 'cap is configured, not fixed at 1 MiB');
  assert.match(p.el('recognition-status').textContent, /辨識上限 8 MiB/);
  p.run('state.recognition.ready = false; state.recognition.unavailable_code = "BROKER_INPUT_BOUND_REQUIRED"; refreshControls()');
  assert.equal(p.el('recognize').disabled, true);
  assert.match(p.el('recognition-status').textContent, /AI 辨識尚未設定完成.*仍可上傳 PDF/);
  assert.doesNotMatch(p.el('recognition-status').textContent, /Broker|direct|token/);
  p.el('pdf').files = [{name: 'upload.pdf', size: 2 * MiB}];
  p.run('refreshControls()');
  assert.equal(p.el('upload').disabled, false, 'unready recognition does not disable upload');
  await p.run('recognize()');
  assert.deepEqual(calls, []);

  p.context.config = {...config, max_pdf_bytes: 8 * MiB};
  p.run('state.recognition = config');
  for (const [job, copy] of [
    [{...complete, state: 'unknown', broker: {phase: 'unknown'}}, /結果未知/],
    [{...complete, state: 'failed', broker: {phase: 'failed'}, error_code: 'BROKER_REJECTED'}, /未完成/],
    [{...complete, state: 'failed', broker: {phase: 'expired'}, error_code: 'BROKER_RESULT_EXPIRED'}, /已到期/],
    [{...complete, state: 'failed', broker: {phase: 'complete', response_truncated: true}, error_code: 'BROKER_RESULT_INCOMPLETE'}, /不完整/],
  ]) {
    p.context.fixtureJob = job;
    p.run('state.jobs = [fixtureJob]; selectJob(fixtureJob.id)');
    assert.match(p.el('job-status').textContent, copy);
    assert.doesNotMatch(p.el('job-status').textContent, /Broker|direct|token/);
    assert.equal(p.el('recognize').disabled, true);
    assert.equal(p.el('refresh-job').hidden, false);
    const before = p.uuidCount();
    await p.run('recognize()');
    assert.equal(p.uuidCount(), before, 'uncertain original job cannot mint another paid-job key');
    assert.deepEqual(calls, []);
    p.context.api = async (path, options) => {calls.push({path, options}); return job;};
    await p.run('queryOriginalJob()');
    assert.equal(calls[0].path, `management/jobs/${job.id}`);
    assert.equal(calls[0].options, undefined, 'recovery queries the original job without POST');
    calls = [];
  }
  p.context.fixtureJob = {...complete, state: 'unknown', broker: {phase: 'unknown'}};
  p.run('state.jobs = [fixtureJob]; state.recognition = {mode: "direct", ready: true, max_pdf_bytes: 8388608}; state.key = true; refreshControls()');
  assert.equal(p.el('recognize').disabled, true, 'switching server config cannot bypass original Broker job');
  const beforeDirectUnknown = p.uuidCount();
  await p.run('recognize()');
  assert.deepEqual(calls, []);
  assert.equal(p.uuidCount(), beforeDirectUnknown);
  p.run('state.jobs = []; state.key = false; refreshControls()');
  assert.equal(p.el('direct-key-settings').hidden, false);
  assert.equal(p.el('recognize').disabled, true, 'direct mode retains personal key requirement');
  p.run('state.key = true; refreshControls()');
  assert.equal(p.el('recognize').disabled, false);
  p.context.fixtureJob = complete;
  p.run('state.jobs = [fixtureJob]; selectJob(fixtureJob.id)');
  assert.equal(p.el('recognize').disabled, false);
  p.context.window.confirm = text => {confirmation = text; return false;};
  await p.run('recognize()');
  assert.deepEqual(calls, []);
  assert.match(confirmation, /新的辨識工作.*Google.*個人金鑰.*額度/);
  assert.doesNotMatch(confirmation, /Broker|direct|token/);
  const directResult = {...complete, id: '33333333-3333-4333-8333-333333333333', execution_mode: 'direct'};
  p.context.window.confirm = () => true;
  p.context.api = async (path, options) => {calls.push({path, options}); return directResult;};
  await p.run('recognize()');
  assert.equal(calls.length, 1);
  const directRepeat = JSON.parse(calls[0].options.body);
  assert.equal(directRepeat.new_attempt, true, 'completed Broker work allows an explicitly confirmed direct business attempt');
  assert.notEqual(directRepeat.request_key, repeat.request_key);

  const t = page(false);
  t.context.snapshot = {...snapshot, documents: [{...doc}], recognition: {...config}, version: 'fixture', max_pdf_bytes: 8 * MiB};
  calls = [];
  t.context.api = async path => {
    calls.push(path);
    return {status: 200, data: path === 'bootstrap' ? t.context.snapshot : {documents: []}};
  };
  await t.run('initialize()');
  assert.equal(t.el('workspace').hidden, false);
  assert.equal(t.el('real-button').disabled, false);
  assert.equal(t.el('direct-key-settings').hidden, true);
  calls = [];
  await t.run('saveKey()'); await t.run('clearKey()'); await t.run('checkKey()');
  assert.deepEqual(calls, [], 'Broker cannot run personal-key text checks');
  t.context.fixtureJob = complete;
  t.context.api = async (path, options) => {calls.push({path, options}); return {status: 200, data: complete};};
  t.run('pollJob = async () => {}');
  await t.el('real-button').listeners.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, 'jobs');
  assert.equal(JSON.parse(calls[0].options.body).scenario, 'real');
  const initialDiagnosticKey = JSON.parse(calls[0].options.body).request_key;
  assert.match(t.el('real-status').textContent, /辨識完成/);
  assert.doesNotMatch(t.el('real-status').textContent, /用量未知|Broker|direct|token/);
  assert.match(t.el('report').value, /Broker 用量：未知/);
  calls = [];
  t.context.window.confirm = text => {confirmation = text; return false;};
  await t.el('rerun-button').listeners.click();
  assert.deepEqual(calls, []);
  assert.match(confirmation, /新的辨識工作.*Google.*額度/);
  t.context.window.confirm = () => true;
  await t.el('rerun-button').listeners.click();
  assert.equal(calls.length, 1);
  const diagnosticRepeat = JSON.parse(calls[0].options.body);
  assert.equal(diagnosticRepeat.new_attempt, true);
  assert.notEqual(diagnosticRepeat.request_key, initialDiagnosticKey);
  calls = [];
  t.run('currentDocument.size += 1; basicOk = true; updateRealControls()');
  assert.equal(t.el('real-button').disabled, true);
  t.el('pdf-file').files = [{size: 2 * MiB, name: 'upload.pdf'}];
  t.run('updateUploadChoice()');
  assert.equal(t.el('upload-button').disabled, false, 'diagnostic upload keeps 8 MiB even with 1 MiB recognition cap');
  await t.run('recognize(true, true)');
  assert.deepEqual(calls, []);
  t.run('recognitionConfig.max_pdf_bytes = 8388608; updateRealControls()');
  assert.equal(t.el('real-button').disabled, false);
  for (const [job, copy] of [
    [{...complete, state: 'running', broker: {phase: 'pending'}}, /已排隊/],
    [{...complete, state: 'unknown', broker: {phase: 'unknown'}}, /結果未知/],
    [{...complete, state: 'failed', broker: {phase: 'failed'}, error_code: 'BROKER_REJECTED'}, /未完成/],
    [{...complete, state: 'failed', broker: {phase: 'expired'}, error_code: 'BROKER_RESULT_EXPIRED'}, /已到期/],
    [{...complete, state: 'done', broker: {phase: 'complete', response_truncated: true}}, /不完整/],
  ]) {
    t.context.fixtureJob = job;
    t.run('jobs = [fixtureJob]; showJob(fixtureJob)');
    assert.match(t.el('ai-status').textContent, copy);
    assert.doesNotMatch(t.el('ai-status').textContent, /Broker|direct|token/);
    assert.equal(t.el('real-button').disabled, true);
    assert.equal(t.el('rerun-button').disabled, true);
    const before = t.uuidCount();
    await t.run('runReal()'); await t.run('recognize(true, true)'); await t.run('recognize(true, false)');
    assert.equal(t.uuidCount(), before);
    assert.deepEqual(calls, []);
    t.context.api = async (path, options) => {calls.push({path, options}); return {status: 200, data: job};};
    await t.run('queryOriginalJob()');
    assert.equal(calls[0].path, `jobs/${job.id}`);
    assert.equal(calls[0].options?.method, undefined);
    assert.equal(calls[0].options?.body, undefined);
    calls = [];
  }
  t.context.fixtureJob = {...complete, state: 'unknown', broker: {phase: 'unknown'}};
  t.run('recognitionConfig = {mode: "direct", ready: true, max_pdf_bytes: 8388608}; aiKeyConfigured = true; jobs = [fixtureJob]; showJob(fixtureJob)');
  const diagnosticUnknownCount = t.uuidCount();
  await t.el('real-button').listeners.click();
  await t.run('recognize(true, true)');
  assert.deepEqual(calls, []);
  assert.equal(t.uuidCount(), diagnosticUnknownCount);
  t.context.fixtureJob = complete;
  t.run('jobs = [fixtureJob]; showJob(fixtureJob)');
  t.context.window.confirm = text => {confirmation = text; return false;};
  await t.el('real-button').listeners.click();
  assert.deepEqual(calls, []);
  assert.match(confirmation, /新的辨識工作.*Google.*個人金鑰.*額度/);
  assert.doesNotMatch(confirmation, /Broker|direct|token/);
  t.context.window.confirm = () => true;
  t.context.api = async (path, options) => {calls.push({path, options}); return {status: 200, data: directResult};};
  await t.el('real-button').listeners.click();
  assert.equal(calls.length, 1);
  const directDiagnosticRepeat = JSON.parse(calls[0].options.body);
  assert.equal(directDiagnosticRepeat.new_attempt, true);
  assert.notEqual(directDiagnosticRepeat.request_key, diagnosticRepeat.request_key);
  calls = [];
  t.run('recognitionConfig.mode = "broker"; jobs = []; currentJob = null; jobSubmitUnknown = false; updateRealControls()');
  t.context.api = async (path, options) => {calls.push({path, options}); throw {code: 'BAD_JSON_RESPONSE', status: 502};};
  await t.el('real-button').listeners.click();
  assert.equal(calls.length, 1);
  const uncertainKeyCount = t.uuidCount();
  await t.el('real-button').listeners.click();
  assert.equal(calls.length, 1, 'a missing Broker receipt cannot be restarted with another paid-job key');
  assert.equal(t.uuidCount(), uncertainKeyCount);
  assert.equal(t.el('real-button').disabled, true);
  assert.match(t.el('ai-status').textContent, /還不能確認/);
  console.log('Broker UI: no personal key, configured PDF cap, readiness, usage unknown, original-job recovery and confirmed completed rerun: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
