// Scoped, browser-local XLSX reader. It never fetches or uploads workbook bytes.
const fail = () => { throw new Error("XLSX_UNSUPPORTED"); };
const decoder = new TextDecoder("utf-8", {fatal: true});
const u16 = (view, at) => view.getUint16(at, true);
const u32 = (view, at) => view.getUint32(at, true);
const children = (node, name) => [...node.children].filter(item => item.localName === name);
const child = (node, name) => children(node, name)[0];
const xml = bytes => {
  const source = decoder.decode(bytes);
  if (/<!DOCTYPE|<!ENTITY/i.test(source)) fail();
  const document = new DOMParser().parseFromString(source, "application/xml");
  if (document.getElementsByTagName("parsererror").length) fail();
  return document;
};
function zipEntries(bytes) {
  if (bytes.byteLength < 22 || bytes.byteLength > 2 * 1024 * 1024) fail();
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let end = -1;
  for (let at = bytes.length - 22; at >= Math.max(0, bytes.length - 65557); at--) {
    if (u32(view, at) === 0x06054b50 && at + 22 + u16(view, at + 20) === bytes.length) {
      end = at; break;
    }
  }
  if (end < 0 || u16(view, end + 8) !== u16(view, end + 10) ||
      u16(view, end + 8) > 200 || u16(view, end + 4) !== 0 || u16(view, end + 6) !== 0) fail();
  let at = u32(view, end + 16);
  const stop = at + u32(view, end + 12);
  if (stop > end) fail();
  const entries = new Map();
  let total = 0;
  for (let index = 0; index < u16(view, end + 10); index++) {
    if (at + 46 > stop || u32(view, at) !== 0x02014b50) fail();
    const flags = u16(view, at + 8), method = u16(view, at + 10);
    const compressed = u32(view, at + 20), size = u32(view, at + 24);
    const nameSize = u16(view, at + 28), extraSize = u16(view, at + 30), commentSize = u16(view, at + 32);
    const offset = u32(view, at + 42);
    if (flags & 1 || ![0, 8].includes(method) || compressed === 0xffffffff || size === 0xffffffff ||
        at + 46 + nameSize + extraSize + commentSize > stop) fail();
    const name = decoder.decode(bytes.subarray(at + 46, at + 46 + nameSize));
    if (!name || name.startsWith("/") || name.split("/").includes("..") || entries.has(name) ||
        /(^|\/)externalLinks\/|vbaProject\.bin$/i.test(name)) fail();
    total += size;
    if (total > 20 * 1024 * 1024) fail();
    if (offset + 30 > bytes.length || u32(view, offset) !== 0x04034b50) fail();
    const start = offset + 30 + u16(view, offset + 26) + u16(view, offset + 28);
    if (start + compressed > bytes.length) fail();
    entries.set(name, {method, size, data: bytes.subarray(start, start + compressed)});
    at += 46 + nameSize + extraSize + commentSize;
  }
  if (at !== stop) fail();
  return entries;
}
async function readEntry(entries, name) {
  const entry = entries.get(name);
  if (!entry) fail();
  if (!entry.method) {
    if (entry.data.length !== entry.size) fail();
    return entry.data;
  }
  if (typeof DecompressionStream !== "function") fail();
  const stream = new Blob([entry.data]).stream().pipeThrough(new DecompressionStream("deflate-raw"));
  const reader = stream.getReader();
  const chunks = []; let total = 0;
  for (;;) {
    const {value, done} = await reader.read();
    if (done) break;
    total += value.length;
    if (total > entry.size) {await reader.cancel(); fail();}
    chunks.push(value);
  }
  if (total !== entry.size) fail();
  const output = new Uint8Array(total); let at = 0;
  for (const chunk of chunks) {output.set(chunk, at); at += chunk.length;}
  return output;
}
function textOf(node) { return [...node.getElementsByTagName("*")]
  .filter(item => item.localName === "t").map(item => item.textContent || "").join(""); }
function sheetPath(workbook, relationships) {
  const sheets = [...workbook.getElementsByTagName("*")].filter(item => item.localName === "sheet");
  if (sheets.length !== 1) fail();
  const relId = sheets[0].getAttribute("r:id") || sheets[0].getAttributeNS(
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships", "id");
  const relation = [...relationships.getElementsByTagName("*")].find(item =>
    item.localName === "Relationship" && item.getAttribute("Id") === relId);
  const target = relation?.getAttribute("Target");
  if (!target || relation.getAttribute("TargetMode") === "External") fail();
  const path = target.startsWith("/") ? target.slice(1) : `xl/${target}`;
  if (!/^xl\/worksheets\/[^/.]+\.xml$/.test(path)) fail();
  return path;
}
export async function readXlsx(file) {
  if (!file || !/\.xlsx$/i.test(file.name) || file.size > 2 * 1024 * 1024 || file.size === 0) fail();
  const bytes = new Uint8Array(await file.arrayBuffer());
  const entries = zipEntries(bytes);
  const workbook = xml(await readEntry(entries, "xl/workbook.xml"));
  const relations = xml(await readEntry(entries, "xl/_rels/workbook.xml.rels"));
  const path = sheetPath(workbook, relations);
  const shared = entries.has("xl/sharedStrings.xml") ?
    [...xml(await readEntry(entries, "xl/sharedStrings.xml")).getElementsByTagName("*")]
      .filter(item => item.localName === "si").map(textOf) : [];
  const worksheet = xml(await readEntry(entries, path));
  if ([...worksheet.getElementsByTagName("*")].some(item => item.localName === "f")) fail();
  const rows = [...worksheet.getElementsByTagName("*")].filter(item => item.localName === "row");
  if (!rows.length || rows.length > 201) fail();
  const result = [];
  for (const row of rows) {
    const number = Number(row.getAttribute("r"));
    if (!Number.isInteger(number) || number < 1 || number > 201 || result[number - 1]) fail();
    const values = [];
    for (const cell of children(row, "c")) {
      const match = /^([A-Z]{1,2})[1-9][0-9]*$/.exec(cell.getAttribute("r") || "");
      if (!match) fail();
      let column = 0;
      for (const letter of match[1]) column = column * 26 + letter.charCodeAt(0) - 64;
      if (column > 30 || values[column - 1] !== undefined) fail();
      const kind = cell.getAttribute("t") || "n";
      const raw = child(cell, "v")?.textContent || "";
      if (kind === "s") {
        const index = Number(raw);
        if (!Number.isInteger(index) || index < 0 || index >= shared.length) fail();
        values[column - 1] = shared[index];
      } else if (kind === "inlineStr") values[column - 1] = textOf(cell);
      else if (kind === "str" || kind === "n") values[column - 1] = raw;
      else fail();
      if (values[column - 1].length > 500) fail();
    }
    result[number - 1] = Array.from({length: values.length}, (_, index) => values[index] ?? "");
  }
  if (result.some(row => !row) || result.length < 2) fail();
  return result;
}
