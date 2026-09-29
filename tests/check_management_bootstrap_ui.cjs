const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const snapshot = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync('web/manage.js', 'utf8').replace(/\nload\(\);\s*$/, '');
const elements = new Map();
function makeElement() {
  return {textContent:'', innerHTML:'', value:'', dataset:{}, files:[], style:{},
    hidden:false, disabled:false, children:[], listeners:{},
    append(...items) {this.children.push(...items);},
    replaceChildren(...items) {this.children=items; this.textContent='';},
    addEventListener(name, callback) {this.listeners[name]=callback;},
    setAttribute(name, value) {this[name]=value;}};
}
function el(id) {if (!elements.has(id)) elements.set(id, makeElement()); return elements.get(id);}
const context = vm.createContext({document:{getElementById:el, createElement:makeElement},
  window:{confirm:()=>true, addEventListener(){}},
  crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},
  fetch:async()=>{throw Error('unexpected network call');},
  AbortController, setTimeout, clearTimeout, Date, console});
vm.runInContext(source, context);
context.snapshot = snapshot;
vm.runInContext('showWorkspace(snapshot)', context);
const orders = snapshot.record_sets.find(item=>item.kind==='purchase_order').rows;
const invoices = snapshot.record_sets.find(item=>item.kind==='invoice').rows;
assert.equal(orders.length, 2);
assert.equal(invoices.length, 3);
assert.match(el('order-rows').innerHTML, / PO Product A /);
assert.match(el('order-rows').innerHTML, /PO Product B/);
assert.match(el('invoice-rows').innerHTML, /INV-1/);
assert.match(el('invoice-rows').innerHTML, /INV-2/);
assert.match(el('invoice-rows').innerHTML, /INV-3/);
assert.match(el('metric-invoices').textContent, /USD/);
assert.match(el('metric-invoices').textContent, /KRW/);
assert.match(el('sales-total').textContent, /USD/);
assert.match(el('sales-total').textContent, /KRW/);
assert.match(el('comparison-rows').innerHTML, /7\.25/);
assert.match(el('comparison-rows').innerHTML, /3\.5/);
assert.match(el('comparison-rows').innerHTML, /未對應發票/);
const csv = vm.runInContext('csvText()', context);
assert.ok(csv.startsWith('\uFEFF'));
assert.match(csv, /"'=PO-1"/);
assert.match(csv, /" PO Product A "/);
assert.match(csv, /"INV-3"/);
assert.equal(csv.trimEnd().split('\r\n').length, 6);
console.log('HTTP bootstrap to UI lists, currency reports, comparison and CSV: ok');
