// Browser-only, strict text-layer parser for 科雅「採購憑單」. No OCR or network calls.
export const PARSER_ID = "koya-purchase-v1";
const number = /^(?:0|[1-9]\d{0,11})(?:\.\d{2})?$/;
const codePattern = /^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$/;
const datePattern = /^\d{4}\/\d{1,2}\/\d{1,2}$/;

function fail(code) {
  const error = new Error(code);
  error.code = code;
  throw error;
}
function clean(value) { return String(value || "").normalize("NFKC").trim().replace(/\s+/g, " "); }
function cents(value) {
  const plain = value.replaceAll(",", "");
  if (!number.test(plain)) fail("LOCAL_NUMBER_UNSUPPORTED");
  const [whole, fraction = ""] = plain.split(".");
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, "0"));
}
function decimal(value) {
  const plain = value.replaceAll(",", "");
  cents(plain);
  return plain;
}
function iso(value) {
  if (!datePattern.test(value)) fail("LOCAL_DATE_UNSUPPORTED");
  const [y, m, d] = value.split("/").map(Number);
  const parsed = new Date(Date.UTC(y, m - 1, d));
  if (parsed.getUTCFullYear() !== y || parsed.getUTCMonth() !== m - 1 || parsed.getUTCDate() !== d)
    fail("LOCAL_DATE_UNSUPPORTED");
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
function one(items, predicate, code = "LOCAL_LAYOUT_UNSUPPORTED") {
  const found = items.filter(predicate);
  if (found.length !== 1) fail(code);
  return found[0];
}
function atLine(items, y, minX, maxX) {
  return items.filter(item => Math.abs(item.y - y) <= 2.5 && item.x >= minX && item.x < maxX);
}
function labelAt(items, label, minX, maxX, y = null) {
  const lines = [...new Set(items.filter(item => item.x >= minX && item.x < maxX &&
    (y === null || Math.abs(item.y - y) <= 2.5)).map(item => item.y))];
  const found = lines.filter(lineY => atLine(items, lineY, minX, maxX)
    .sort((a, b) => a.x - b.x).map(item => item.s).join("").replaceAll(" ", "") === label);
  if (found.length !== 1) fail("LOCAL_LAYOUT_UNSUPPORTED");
  return {y: found[0]};
}
function joinDescription(parts) {
  return parts.reduce((result, part) => {
    if (!result) return part;
    return result + ((/\([A-Z]$/.test(result) && /^[A-Z]\)/.test(part) || /[\d-]$/.test(result) && /^(?:\d|[A-Z]{1,2}$)/.test(part)) ? "" : " ") + part;
  }, "");
}
export function parseKoyaText({items, pageCount, pageWidth, pageHeight}) {
  if (pageCount !== 1) fail("LOCAL_PAGE_COUNT_UNSUPPORTED");
  if (!Number.isFinite(pageWidth) || !Number.isFinite(pageHeight) ||
      pageWidth < 590 || pageWidth > 600 || pageHeight < 835 || pageHeight > 850)
    fail("LOCAL_PAGE_SIZE_UNSUPPORTED");
  if (!Array.isArray(items) || items.length < 40 || items.length > 5000) fail("LOCAL_TEXT_UNAVAILABLE");
  const text = items.map(item => ({s: clean(item.str ?? item.s), x: Number(item.x), y: Number(item.y)}))
    .filter(item => item.s && Number.isFinite(item.x) && Number.isFinite(item.y));
  if (!text.some(item => item.s === "科雅先端股份有限公司") ||
      !text.some(item => item.s === "採購憑單")) fail("LOCAL_TEMPLATE_UNSUPPORTED");
  const header = labelAt(text, "產品編號", 0, 110);
  for (const [label, left, right] of [["品名規格", 110, 245], ["數量", 245, 289],
    ["單位", 289, 330], ["單價", 330, 410], ["金額", 410, 455],
    ["預進貨日", 455, 530]]) labelAt(text, label, left, right, header.y);
  const purchaseLabel = labelAt(text, "採購日期:", 0, 60);
  const numberLabel = labelAt(text, "採購單號:", 400, 465);
  const date = iso(one(atLine(text, purchaseLabel.y, 60, 130), item => datePattern.test(item.s)).s);
  const orderNo = one(atLine(text, numberLabel.y, 460, 540), item => /^\d{8,20}$/.test(item.s)).s;
  const currencyLabel = one(text, item => item.s === "別:" && item.x < 60);
  const currency = one(atLine(text, currencyLabel.y, 60, 110), item => /^[A-Z]{3}$/.test(item.s)).s;
  const supplierLabel = one(text, item => item.s === "廠商名稱:" && item.x < 100);
  const supplier = one(atLine(text, supplierLabel.y, 60, 220), item => item.s !== supplierLabel.s).s;
  const totalLabel = one(text, item => item.s === "總計:" && item.x > 320);
  const sumLabel = one(text, item => item.s === "數量合計:" && item.x > 150);
  if (totalLabel.y >= header.y || sumLabel.y >= header.y) fail("LOCAL_LAYOUT_UNSUPPORTED");
  const entries = text.filter(item => item.x < 110 && item.y < header.y - 3 && item.y > totalLabel.y + 3 && codePattern.test(item.s))
    .sort((a, b) => b.y - a.y);
  if (!entries.length || entries.length > 100 || new Set(entries.map(item => item.s)).size !== entries.length)
    fail("LOCAL_ROWS_UNSUPPORTED");
  const rows = entries.map((entry, index) => {
    const nextY = entries[index + 1]?.y ?? sumLabel.y;
    const line = atLine(text, entry.y, 0, 530);
    const qty = one(line, item => item.x >= 245 && item.x < 289 && /^\d+$/.test(item.s)).s;
    if (BigInt(qty) <= 0n) fail("LOCAL_ROWS_UNSUPPORTED");
    const unitItems = line.filter(item => item.x >= 289 && item.x < 330);
    if (unitItems.length > 1 || unitItems.some(item => !/^[A-Z]{1,8}$/.test(item.s))) fail("LOCAL_LAYOUT_UNSUPPORTED");
    const unit = unitItems[0]?.s ?? null;
    const price = decimal(one(line, item => item.x >= 330 && item.x < 410).s);
    const amountAndArrival = one(line, item => item.x >= 410 && item.x < 530).s
      .match(/^([\d,]+\.\d{2})\s+(\d{4}\/\d{1,2}\/\d{1,2})$/);
    if (!amountAndArrival) fail("LOCAL_LAYOUT_UNSUPPORTED");
    const amount = decimal(amountAndArrival[1]);
    const arrival = iso(amountAndArrival[2]);
    if (BigInt(qty) * cents(price) !== cents(amount)) fail("LOCAL_ROW_TOTAL_MISMATCH");
    const description = text.filter(item => item.x >= 110 && item.x < 245 &&
      item.y <= entry.y + 2.5 && item.y > nextY + 2.5)
      .sort((a, b) => b.y - a.y || a.x - b.x).map(item => item.s);
    if (!description.length) fail("LOCAL_ROWS_UNSUPPORTED");
    return {orderNo, client: "科雅先端股份有限公司", product: joinDescription(description),
      code: entry.s, qty, unit, unitPrice: price, amount, currency, date,
      incoterms: null, expectedArrival: arrival};
  });
  const reportedQty = one(atLine(text, sumLabel.y, 245, 330), item => /^\d+$/.test(item.s)).s;
  const reportedSubtotal = one(atLine(text, sumLabel.y, 410, 530), item => /^[\d,]+\.\d{2}$/.test(item.s)).s;
  const taxLabel = labelAt(text, "稅金:", 330, 410);
  const reportedTax = one(atLine(text, taxLabel.y, 410, 530), item => /^[\d,]+\.\d{2}$/.test(item.s)).s;
  const reportedTotal = one(atLine(text, totalLabel.y, 410, 530), item => /^[\d,]+\.\d{2}$/.test(item.s)).s;
  const calculated = rows.reduce((sum, row) => sum + cents(row.amount), 0n);
  if (rows.reduce((sum, row) => sum + BigInt(row.qty), 0n) !== BigInt(reportedQty) ||
      calculated !== cents(reportedSubtotal) || cents(reportedTax) !== 0n ||
      calculated !== cents(reportedTotal)) fail("LOCAL_DOCUMENT_TOTAL_MISMATCH");
  const remarkLabel = text.find(item => item.s === "備註:" && item.x < 60);
  const remark = remarkLabel ? atLine(text, remarkLabel.y, 45, 350).map(item => item.s).join(" ") : "";
  return {parserId: PARSER_ID, kind: "purchase_order", rows: rows.map(({expectedArrival, ...row}) => row),
    hints: {supplier, remark, expectedArrivals: [...new Set(rows.map(row => row.expectedArrival))],
      quantityTotal: reportedQty, documentTotal: decimal(reportedTotal), missingUnits: rows.filter(row => row.unit === null).length}};
}
