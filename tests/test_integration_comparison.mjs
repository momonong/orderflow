import assert from "node:assert/strict";
import {summarizeItems} from "../web/integration-comparison.mjs";

const doc = (id, kind, number, quantity, unit = "EA") => ({id, kind,
  confirmed: {header: {number}, rows: [{id: `${id}-row`, code: id, description: id, quantity, unit}]}});
const invoice = doc("invoice", "invoice", "INV-1", "6");
const poA = doc("po-a", "purchase_order", "PO-A", "10");
const poB = doc("po-b", "purchase_order", "PO-B", "10");
const link = (id, po, quantity) => ({id, invoice_document_id: invoice.id,
  invoice_item_id: "invoice-row", po_document_id: po.id, po_item_id: `${po.id}-row`, quantity});

const rows = summarizeItems([invoice, poA, poB], [link("a", poA, "4"), link("b", poB, "2")]);
assert.deepEqual(rows.map(row => [row.allocated, row.remaining, row.status]), [
  ["6", "0", "已完全分配"], ["4", "6", "部分對應"], ["2", "8", "部分對應"]]);
assert.equal(summarizeItems([invoice, poA], [link("a", poA, null)])[0].status, "未知／不可比");
assert.equal(summarizeItems([doc("x", "invoice", "INV-2", "1", "")], [])[0].remaining, null);
assert.equal(summarizeItems([invoice], [])[0].status, "未對應");
console.log("integration allocation summary and unknown boundaries: ok");
