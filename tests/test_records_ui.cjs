const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/index.html', 'utf8');
const source = fs.readFileSync('web/manage.js', 'utf8').replace(/\nload\(\);\s*$/, '');
for (const id of ['page-dashboard', 'page-upload', 'page-orders', 'page-invoices',
  'page-sales', 'page-comparison', 'purchase-pdf', 'invoice-pdf', 'csv-export']) {
  assert.match(html, new RegExp(`id="${id}"`));
}
assert.doesNotMatch(html, />缺貨<|>可用庫存<|>已出貨</);
const elements = new Map();
function makeElement() {
  return {textContent: '', innerHTML: '', value: '', dataset: {}, files: [], style: {},
    hidden: false, disabled: false, children: [], listeners: {},
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children = items; this.textContent = '';},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    setAttribute(name, value) {this[name] = value;}};
}
function el(id) {if (!elements.has(id)) elements.set(id, makeElement()); return elements.get(id);}
const context = vm.createContext({
  document: {getElementById: el, createElement: makeElement},
  window: {confirm: () => true, addEventListener() {}},
  crypto: {randomUUID: () => '00000000-0000-4000-8000-000000000001'},
  fetch: async () => {throw Error('unexpected fetch');},
  AbortController, setTimeout, clearTimeout, Date, console,
});
vm.runInContext(source, context);
const orderId = '10000000-0000-4000-8000-000000000001';
const invoiceId = '20000000-0000-4000-8000-000000000001';
const orderRowId = '30000000-0000-4000-8000-000000000001';
const invoiceRowId = '40000000-0000-4000-8000-000000000001';
const jobId = '50000000-0000-4000-8000-000000000001';
const order = {id:orderRowId, orderNo:'=PO-1', invoiceNo:null, client:'客戶 <A>',
  product:' Part // [β]  _ ', code:'P-1', qty:'10.50', unit:'PCS', unitPrice:'2.00',
  amount:'21.00', currency:'USD', date:'2026-09-29', incoterms:'DAP', status:'確認中',
  linked_order_row_id:null, deleted:false};
const invoice = {id:invoiceRowId, orderNo:null, invoiceNo:'INV-1', client:'客戶 <A>',
  product:' Part // [β]  _ ', code:'P-1', qty:'3.25', unit:'PCS', unitPrice:'2.00',
  amount:'6.50', currency:'USD', date:'2026-09-29', incoterms:'DAP', status:null,
  linked_order_row_id:orderRowId, deleted:false};
const secondInvoice = {...invoice, id:'40000000-0000-4000-8000-000000000002',
  invoiceNo:'INV-2', currency:'KRW', amount:'1200.00', qty:null,
  linked_order_row_id:null};
const snapshot = {documents:[{id:orderId, document_kind:'purchase_order', size:500, created_ms:1},
  {id:invoiceId, document_kind:'invoice', size:500, created_ms:2}],
  jobs:[{id:jobId, document_id:orderId, state:'done', scenario:'real',
    result:[{product:' Part // [β]  _ ', qty:'10.50'}], created_ms:3}],
  drafts:[], record_sets:[{document_id:orderId, source_job_id:jobId, kind:'purchase_order',
    rows:[order], revision:1}, {document_id:invoiceId, source_job_id:'60000000-0000-4000-8000-000000000001',
    kind:'invoice', rows:[invoice, secondInvoice], revision:1}], ai_key_configured:false};
context.snapshot = snapshot;
vm.runInContext('showWorkspace(snapshot)', context);
assert.match(el('metric-orders').textContent, /21 USD/);
assert.match(el('metric-invoices').textContent, /6\.5 USD/);
assert.match(el('metric-invoices').textContent, /1,200 KRW/);
assert.equal(el('metric-invoice-qty').textContent, '—', 'unknown invoice qty cannot become zero');
assert.match(el('order-rows').innerHTML, /客戶 &lt;A&gt;/);
assert.doesNotMatch(el('order-rows').innerHTML, /客戶 <A>/);
assert.match(el('comparison-rows').innerHTML, /7\.25/);
assert.match(el('comparison-rows').innerHTML, /未對應發票/);
assert.match(el('sales-total').textContent, /USD/);
assert.match(el('sales-total').textContent, /KRW/);
el('order-search').value = 'not-present';
vm.runInContext('renderOrders()', context);
assert.equal(el('order-count').textContent, '0 筆');
el('order-search').value = '';
el('order-status-filter').value = 'confirmed';
vm.runInContext('renderOrders()', context);
assert.equal(el('order-count').textContent, '0 筆');
el('order-status-filter').value = 'pending';
vm.runInContext('renderOrders()', context);
assert.equal(el('order-count').textContent, '1 筆');
el('invoice-search').value = 'not-present';
vm.runInContext('renderInvoices()', context);
assert.equal(el('invoice-count').textContent, '0 筆');
el('invoice-search').value = '';
context.fixedDate = new Date(2026, 8, 29);
context.invoice = invoice;
assert.equal(vm.runInContext('rowDateForPeriod(invoice, "month", fixedDate)', context), true);
assert.equal(vm.runInContext('rowDateForPeriod(invoice, "quarter", fixedDate)', context), true);
assert.equal(vm.runInContext('rowDateForPeriod({...invoice, date:null}, "month", fixedDate)', context), false);
const csv = vm.runInContext('csvText()', context);
assert.ok(csv.startsWith('\uFEFF'));
assert.match(csv, /"'=PO-1"/);
assert.match(csv, /" Part \/\/ \[β\]  _ "/);
assert.match(csv, /"客戶 <A>"/);
assert.match(csv, /\r\n/);
context.synthetic = {client:'A', amount:null, currency:null};
assert.equal(vm.runInContext('moneySummary([{row:synthetic}])', context), '1 筆缺金額／幣別');
assert.equal(vm.runInContext('formatDecimal(null)', context), '—');
assert.equal(vm.runInContext('formatUnits(-72500n)', context), '−7.25');
context.order = order;
assert.match(vm.runInContext('linkIssue({...invoice, client:"別的客戶"}, order)', context), /客戶/);
assert.equal(vm.runInContext('linkIssue(invoice, order)', context), null);
vm.runInContext('showPage("invoices")', context);
assert.equal(vm.runInContext('state.page', context), 'invoices');
const calls = [];
context.api = async (path, options) => {
  calls.push({path, method:options.method, body:JSON.parse(options.body)});
  const set = snapshot.record_sets.find(item => item.document_id === path.split('/').at(-1));
  return {...set, rows:JSON.parse(options.body).rows, revision:set.revision + 1};
};
(async () => {
  vm.runInContext('state.rows[0].amount="22.00"; markDirty()', context);
  await vm.runInContext('saveRecordSet()', context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, `management/record-sets/${orderId}`);
  assert.equal(calls[0].body.revision, 1);
  assert.equal(calls[0].body.rows[0].product, ' Part // [β]  _ ');
  await vm.runInContext(`deleteRecordRow("${invoiceId}", "${invoiceRowId}", "invoice")`, context);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.rows[0].deleted, true);
  assert.equal(calls[1].body.rows[1].deleted, false);
  const uploadKeys = [];
  context.crypto.subtle = {digest: async () => new Uint8Array(32).buffer};
  context.syntheticFile = {name:'sample.pdf', size:4,
    arrayBuffer:async () => new Uint8Array([1,2,3,4]).buffer};
  context.api = async (path, options) => {
    assert.equal(path, 'management/documents');
    assert.equal(options.headers['X-Document-Kind'], 'invoice');
    uploadKeys.push(options.headers['X-Request-Key']);
    throw {code:'REQUEST_TIMEOUT'};
  };
  await vm.runInContext('state.pendingFile=syntheticFile; state.pendingKind="invoice"; upload()', context);
  assert.equal(el('retry-upload').hidden, false);
  await vm.runInContext('upload()', context);
  assert.equal(uploadKeys.length, 2);
  assert.equal(uploadKeys[0], uploadKeys[1], 'uncertain upload retries preserve request key');
  console.log('six-page records, filters, save/delete, retry, totals, comparison and CSV: ok');
})().catch(error => {console.error(error); process.exitCode = 1;});
