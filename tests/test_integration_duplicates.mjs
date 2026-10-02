import assert from "node:assert/strict";
import {suspectedDuplicates} from "../web/integration-duplicates.mjs";

const fields = (company, number) => ({header: {company, number}});
const documents = [
  {id: "original", kind: "purchase_order", file_sha256: "abc",
    confirmed: fields("Synthetic Co", "PO-1")},
  {id: "invoice", kind: "invoice", file_sha256: null,
    confirmed: fields("Synthetic Co", "PO-1")},
];
assert.deepEqual(suspectedDuplicates(documents, null, "purchase_order",
  fields(" synthetic co ", "po-1"), "abc").map(match => [match.doc.id, match.reasons]),
  [["original", ["相同原檔雜湊", "同種類／公司／單號"]]]);
assert.equal(suspectedDuplicates(documents, "original", "purchase_order",
  fields("Synthetic Co", "PO-1"), "abc").length, 0);
assert.equal(suspectedDuplicates(documents, null, "purchase_order",
  fields("", "PO-1"), null).length, 0);
assert.equal(suspectedDuplicates(documents, null, "invoice",
  fields("Synthetic Co", "PO-1"), null)[0].doc.id, "invoice");
console.log("integration duplicate advisory boundaries: ok");
