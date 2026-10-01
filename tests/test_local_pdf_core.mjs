import assert from "node:assert/strict";
import {parseKoyaText} from "../web/local-pdf-core.mjs";

function item(str, x, y) { return {str, x, y}; }
function fixture() {
  return {pageCount: 1, pageWidth: 595, pageHeight: 842, items: [
    item("科雅先端股份有限公司", 204, 779), item("採購憑單", 262, 757),
    item("採購日期:", 19, 718), item("2026/09/23", 70, 718),
    item("採購單號:", 420, 718), item("20260923001", 471, 718),
    item("廠商名稱:", 19, 689), item("CMTX Co.,Ltd.", 70, 689),
    item("別:", 46, 645), item("USD", 70, 645),
    item("備註:", 19, 599), item("2026/10需求", 51, 599),
    item("產品編號", 19, 583), item("品名規格", 135, 583), item("數量", 266, 583),
    item("單位", 290, 583), item("單價", 362, 583), item("金額", 433, 583),
    item("預進貨日", 457, 583),
    item("ABC-001", 19, 568), item("PART,WITH", 135, 568), item("CONTINUATION", 135, 558),
    item("2", 270, 568), item("EA", 290, 568), item("5.00", 356, 568),
    item("10.00 2026/09/23", 422, 568), item("2", 558, 568),
    item("ABC-002", 19, 544), item("NO UNIT", 135, 544), item("3", 270, 544),
    item("4.00", 356, 544), item("12.00 2026/09/23", 422, 544), item("3", 558, 544),
    item("數量合計:", 180, 379), item("5", 271, 379), item("合計:", 334, 379),
    item("22.00", 420, 379), item("稅金:", 334, 364), item("0.00", 443, 364),
    item("總計:", 334, 348), item("22.00", 420, 348),
  ]};
}
const parsed = parseKoyaText(fixture());
assert.equal(parsed.rows.length, 2);
assert.equal(parsed.rows[0].product, "PART,WITH CONTINUATION");
assert.equal(parsed.rows[1].unit, null);
assert.equal(parsed.rows[0].date, "2026-09-23");
assert.equal(parsed.rows[0].incoterms, null);
assert.deepEqual(parsed.hints.expectedArrivals, ["2026-09-23"]);
assert.equal(parsed.hints.remark, "2026/10需求");
function fails(change, code) {
  const input = fixture(); change(input);
  assert.throws(() => parseKoyaText(input), error => error.code === code);
}
fails(data => { data.pageCount = 2; }, "LOCAL_PAGE_COUNT_UNSUPPORTED");
fails(data => { data.items = []; }, "LOCAL_TEXT_UNAVAILABLE");
fails(data => { data.items.find(v => v.str === "採購憑單").str = "其他單據"; }, "LOCAL_TEMPLATE_UNSUPPORTED");
fails(data => { data.items.filter(v => v.str === "22.00").at(-1).str = "23.00"; }, "LOCAL_DOCUMENT_TOTAL_MISMATCH");
fails(data => { data.items.find(v => v.str === "10.00 2026/09/23").str = "11.00 2026/09/23"; }, "LOCAL_ROW_TOTAL_MISMATCH");
fails(data => { data.items.find(v => v.str === "ABC-002").str = "ABC-001"; }, "LOCAL_ROWS_UNSUPPORTED");
fails(data => { data.items.find(v => v.str === "單價").x = 200; }, "LOCAL_LAYOUT_UNSUPPORTED");
console.log("local PDF template, unknown unit, continuation, totals and unsupported cases: ok");
