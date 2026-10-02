import {readXlsx} from "/orderflow/integration-xlsx.mjs";
import {summarizeItems} from "/orderflow/integration-comparison.mjs";
import {suspectedDuplicates} from "/orderflow/integration-duplicates.mjs";
import {createActivityLock} from "/orderflow/integration-activity.mjs";

const $ = id => document.getElementById(id);
const apiRoot = "/orderflow/api/integration/";
const blankRow = () => ({id: crypto.randomUUID(), code: "", description: "", quantity: null,
  unit: "", unit_price: null, amount: null});
const blankFields = () => ({header: {company: "", number: "", date: "", currency: ""}, rows: [blankRow()]});
const copy = value => JSON.parse(JSON.stringify(value));
const state = {documents: [], links: [], products: [], selected: null, fields: blankFields(),
  candidate: null, file: null, fileSha: null, matrix: null, requestKey: crypto.randomUUID(),
  source: "manual", kind: "purchase_order", baseline: "", parseBaseline: "", filters: {}, viewed: [],
  selectedProduct: null, productAliases: [], productBaseline: "", duplicateApproval: null};
const activity = createActivityLock($("workspace"));
const labels = {company: "公司", number: "單號", date: "文件日期", currency: "幣別",
  code: "品號", description: "描述", quantity: "數量", unit: "單位",
  unit_price: "單價", amount: "金額"};
const rowKeys = ["code", "description", "quantity", "unit", "unit_price", "amount"];
function status(message, error = false) { $("status").textContent = message; $("status").dataset.error = String(error); }
function sourceStatus(message, error = false) { $("source-status").textContent = message;
  $("source-status").dataset.error = String(error); }
function codeMessage(code) {
  return ({TRIAL_FIELDS_INVALID: "欄位格式不符；請核對日期、幣別、數量與金額。",
    TRIAL_FILE_INVALID: "原檔格式或雜湊不符。", TRIAL_FILE_TOO_LARGE: "原檔超過限制。",
    TRIAL_STORAGE_LIMIT: "此試用會話的文件數量或儲存空間已達上限。",
    TRIAL_IDEMPOTENCY_CONFLICT: "同一儲存請求的內容不同；請重新開啟文件後再試。",
    TRIAL_VERSION_CONFLICT: "文件已有較新的修訂；畫面保留你的修改，可先核對後手動載入最新版。",
    TRIAL_LINKED_ROW: "已對應品項不能移除。",
    TRIAL_ALLOCATION_CONFLICT: "分配數量超過發票或採購單品項的已填數量。",
    TRIAL_LINK_CONFLICT: "分配數量需要兩側單位相同且有數量。",
    TRIAL_PDF_CHECK_TIMEOUT: "PDF 安全檢查逾時；檔案未保存。",
    TRIAL_PRODUCT_VERSION_CONFLICT: "主檔已有較新修訂；畫面保留你的修改，可先核對後手動載入最新版。",
    TRIAL_ALIAS_CONFLICT: "公司與品號組合已被另一個主檔使用。",
    TRIAL_PRODUCT_INVALID: "主檔描述及每組公司、品號都需完整；最多 20 組。"})[code] || code || "連線或伺服器錯誤";
}
function parseMessage(code) {
  return ({LOCAL_PAGE_COUNT_UNSUPPORTED: "目前只支援單頁科雅採購憑單",
    LOCAL_PAGE_SIZE_UNSUPPORTED: "PDF 尺寸與支援版型不符",
    LOCAL_TEXT_UNAVAILABLE: "找不到可用文字層；不使用 OCR",
    LOCAL_TEMPLATE_UNSUPPORTED: "不是目前支援的科雅採購憑單版型",
    LOCAL_LAYOUT_UNSUPPORTED: "欄位位置或格式與支援版型不同",
    LOCAL_ROWS_UNSUPPORTED: "品項列不完整或重複",
    LOCAL_NUMBER_UNSUPPORTED: "數字格式不受支援",
    LOCAL_DATE_UNSUPPORTED: "文件日期無法可靠辨識",
    LOCAL_ROW_TOTAL_MISMATCH: "品項金額與數量不一致",
    LOCAL_DOCUMENT_TOTAL_MISMATCH: "文件合計與品項不一致",
    PDF_TIMEOUT: "本機 PDF 解析逾時",
    XLSX_UNSUPPORTED: "此 Excel 結構、公式或欄位範圍不受支援"})[code] || "原檔無法可靠解析";
}
async function api(path, method = "GET", body, timeout = 12000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch("/orderflow/api/" + path, {method, credentials: "same-origin",
      cache: "no-store", signal: controller.signal,
      headers: {"X-Orderflow-Request": "1", ...(body ? {"Content-Type": "application/json"} : {})},
      ...(body ? {body: JSON.stringify(body)} : {})});
    const data = await response.json();
    if (response.status === 401) $("login-section").hidden = false;
    if (!response.ok) throw {code: data.error_code || "HTTP_ERROR", status: response.status};
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw {code: "REQUEST_TIMEOUT", unknown: true};
    if (!error.code) throw {code: "NETWORK_ERROR", unknown: true};
    throw error;
  } finally { clearTimeout(timer); }
}
function option(select, value, label) { const item = document.createElement("option");
  item.value = value; item.textContent = label; select.append(item); }
function field(label, value, onInput, maxLength = 300) {
  const wrapper = document.createElement("label"); wrapper.textContent = label;
  const input = document.createElement("input"); input.value = value ?? ""; input.maxLength = maxLength;
  input.addEventListener("input", () => onInput(input.value)); wrapper.append(input); return wrapper;
}
function renderRows() {
  const target = $("rows"); target.replaceChildren();
  state.fields.rows.forEach((row, index) => {
    const card = document.createElement("div"); card.className = "row-card";
    const heading = document.createElement("strong"); heading.textContent = `品項 ${index + 1}`; card.append(heading);
    const fields = document.createElement("div"); fields.className = "row-fields";
    for (const key of rowKeys) {
      const control = field(labels[key], row[key], value => {
        row[key] = ["quantity", "unit_price", "amount"].includes(key) ? value || null : value;
        if (key === "code") renderSuggestion(index);
      }, key === "description" ? 300 : key === "unit" ? 24 : 80);
      fields.append(control);
    }
    const hint = document.createElement("p"); hint.id = `suggestion-${index}`; hint.className = "small";
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "移除此品項";
    remove.addEventListener("click", () => {
      if (state.fields.rows.length <= 1) return status("文件至少保留一個品項。", true);
      state.fields.rows.splice(index, 1); renderRows();
    });
    card.append(fields, hint, remove); target.append(card); renderSuggestion(index);
  });
}
function renderSuggestion(index) {
  const row = state.fields.rows[index], company = state.fields.header.company;
  const hint = $(`suggestion-${index}`); if (!hint) return;
  const match = row.code && company && state.products.find(product => product.aliases.some(alias =>
    alias.company === company && alias.code === row.code));
  hint.textContent = match ? `主檔建議：${match.label}；單位 ${match.unit || "未填"}。原欄位未變更。`
    : "無對應主檔建議；空白品號不自動配對。";
}
function renderEditor() {
  $("editor").hidden = false;
  $("kind").value = state.kind; $("source").value = state.source;
  $("kind").disabled = !!state.selected; $("source").disabled = !!state.selected;
  for (const key of ["company", "number", "date", "currency"]) $("field-" + key).value = state.fields.header[key];
  $("editor-mode").textContent = state.selected ? `編修已存文件；修訂 ${state.selected.revision}。保存時檢查衝突。`
    : "新文件尚未保存；可逐欄修改，按確認後才上傳。";
  $("candidate-view").textContent = state.candidate ? JSON.stringify(state.candidate, null, 2)
    : "沒有成功解析的候選值；請人工逐欄核對。原檔仍可一起保存。";
  const readable = $("candidate-readable"); readable.replaceChildren();
  if (state.candidate) {
    const header = document.createElement("p"); const h = state.candidate.header;
    header.textContent = `公司：${h.company || "未填"}；單號：${h.number || "未填"}；文件日期：${h.date || "未填"}；幣別：${h.currency || "未填"}`;
    readable.append(header);
    for (const [index, row] of state.candidate.rows.entries()) {
      const line = document.createElement("p");
      line.textContent = `品項 ${index + 1}：${row.code || "未填品號"}；${row.description || "未填描述"}；數量 ${row.quantity ?? "未知"} ${row.unit || "未填單位"}；單價 ${row.unit_price ?? "未知"}；金額 ${row.amount ?? "未知"}`;
      readable.append(line);
    }
  } else readable.textContent = "沒有成功解析的候選值；以人工欄位為準。";
  $("open-original").disabled = !(state.file || state.selected?.file_sha256);
  renderRows();
  updateDuplicateHint();
}
function collectFields() {
  for (const key of ["company", "number", "date", "currency"]) state.fields.header[key] = $("field-" + key).value.trim();
  return copy(state.fields);
}
function rememberBaseline() {state.baseline = JSON.stringify(collectFields());}
function hasUnsaved() {return !!state.file || JSON.stringify(collectFields()) !== state.baseline;}
function allowDiscard() {return !hasUnsaved() || window.confirm("目前有未保存的欄位或原檔。確定放棄並切換？");}
function duplicates(fields, fileSha = state.fileSha) {
  return suspectedDuplicates(state.documents, state.selected?.id, state.kind, fields, fileSha);
}
function duplicateText(matches) {
  return matches.map(({doc, reasons}) => `${doc.kind === "invoice" ? "發票" : "採購單"} ${doc.confirmed.header.company || "未填公司"}／${doc.confirmed.header.number || "未填單號"}（${reasons.join("、")}）`).join("；");
}
function updateDuplicateHint() {
  if ($("editor").hidden) return;
  const matches = duplicates(collectFields());
  $("duplicate-hint").textContent = matches.length ? `疑似重複：${duplicateText(matches)}。保存時仍需人工確認。` : "";
}
function clearDuplicateReview() {
  state.duplicateApproval = null; $("duplicate-review").hidden = true;
}
function localValidate(fields) {
  const h = fields.header;
  if (h.date) {
    const match = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$/.exec(h.date);
    if (!match) return "文件日期：請用 YYYY-MM-DD 或 YYYY/M/D。";
    const [year, month, day] = match.slice(1).map(Number);
    const parsed = new Date(Date.UTC(year, month - 1, day));
    if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 ||
        parsed.getUTCDate() !== day) return "文件日期：請填實際存在的日期，或留空。";
  }
  if (h.currency && !/^[A-Za-z]{3}$/.test(h.currency)) return "幣別：請填三個英文字母或留空。";
  for (const [index, row] of fields.rows.entries()) {
    for (const key of ["quantity", "unit_price", "amount"]) {
      const value = row[key];
      if (value !== null && value !== "" && !new RegExp(key === "quantity" ?
        "^(?:0|[1-9][0-9]{0,11})(?:\\.[0-9]{1,4})?$" :
        "^(?:0|[1-9][0-9]{0,11})(?:\\.[0-9]{1,2})?$").test(value))
        return `第 ${index + 1} 筆「${labels[key]}」：需為非負數；${key === "quantity" ? "最多四" : "最多兩"}位小數，或留空。`;
    }
  }
  return null;
}
function newDocument(force = false) {
  if (!force && !allowDiscard()) return;
  clearDuplicateReview();
  state.selected = null; state.file = null; state.fileSha = null; state.matrix = null; state.candidate = null;
  state.fields = blankFields(); state.requestKey = crypto.randomUUID();
  state.source = "manual"; state.kind = "purchase_order";
  $("kind").value = "purchase_order"; $("source").value = "manual";
  $("source-file").value = ""; $("source-file").disabled = true;
  $("mapping").hidden = true; $("reload-document").hidden = true;
  renderEditor(); rememberBaseline(); sourceStatus("人工輸入；尚未保存。");
}
function chooseSource() {
  if (state.selected) {$("source").value = state.source;
    return status("編修已存文件時不能更換原檔來源；請新增文件。", true);}
  if (!allowDiscard()) {$("source").value = state.source; return;}
  clearDuplicateReview();
  state.source = $("source").value; state.file = null; state.fileSha = null;
  state.matrix = null; state.candidate = null;
  $("source-file").value = ""; $("source-file").disabled = state.source === "manual";
  $("source-file").accept = state.source === "pdf" ? ".pdf,application/pdf" : ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  $("parse-file").disabled = true; $("mapping").hidden = true;
  state.fields = blankFields(); renderEditor(); rememberBaseline();
  sourceStatus(state.source === "manual" ? "人工輸入。" : "請選原檔；可解析，或直接人工填寫。尚未上傳。");
}
function fileSelected() {
  if (state.selected) return;
  const replacement = $("source-file").files[0] || null;
  if ((state.file || hasUnsaved()) && !allowDiscard()) {$("source-file").value = "";
    $("parse-file").disabled = !state.file; return;}
  clearDuplicateReview();
  state.file = replacement; state.fileSha = null; state.matrix = null; state.candidate = null;
  $("mapping").hidden = true; $("parse-file").disabled = !state.file;
  if (!state.file) return sourceStatus("尚未選檔。");
  const limit = state.source === "pdf" ? 8 * 1024 * 1024 : 2 * 1024 * 1024;
  const extension = state.source === "pdf" ? ".pdf" : ".xlsx";
  if (!state.file.name.toLowerCase().endsWith(extension) || state.file.size < 1 || state.file.size > limit) {
    state.file = null; $("parse-file").disabled = true;
    return sourceStatus(`只支援不超過 ${state.source === "pdf" ? "8" : "2"} MB 的 ${extension} 原檔。`, true);
  }
  state.fields = blankFields(); state.requestKey = crypto.randomUUID();
  renderEditor(); state.parseBaseline = JSON.stringify(collectFields());
  sourceStatus("原檔留在瀏覽器；按解析或直接人工核對。保存前不會上傳。");
  void hashFile(state.file).then(sha => {if (state.file === replacement) {
    state.fileSha = sha; updateDuplicateHint();
  }}).catch(() => {if (state.file === replacement) sourceStatus("原檔雜湊無法在瀏覽器計算；請重新選檔。", true);});
}
async function parsePdf(file) {
  const bytes = await file.arrayBuffer();
  const worker = new Worker("/orderflow/local-pdf-worker.mjs", {type: "module"});
  try { return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("PDF_TIMEOUT")), 15000);
    worker.onmessage = event => { clearTimeout(timer); event.data?.ok ? resolve(event.data.result)
      : reject(new Error(event.data?.code || "PDF_UNSUPPORTED")); };
    worker.onerror = () => {clearTimeout(timer); reject(new Error("PDF_UNSUPPORTED"));};
    worker.postMessage({bytes}, [bytes]);
  }); } finally {worker.terminate();}
}
async function parseFile() {
  const file = state.file; if (!file || activity.busy) return;
  if (JSON.stringify(collectFields()) !== state.parseBaseline &&
      !window.confirm("重新解析會替換目前人工編修的欄位。確定繼續？")) return;
  const source = state.source;
  await activity.run(async () => {
    sourceStatus("正在瀏覽器解析；尚未上傳…");
    try {
      if (source === "pdf") {
        const parsed = await parsePdf(file);
        state.kind = "purchase_order";
        state.fields = {header: {company: parsed.rows[0]?.client || "", number: parsed.rows[0]?.orderNo || "",
          date: parsed.rows[0]?.date || "", currency: parsed.rows[0]?.currency || ""},
          rows: parsed.rows.map(row => ({id: crypto.randomUUID(), code: row.code || "",
            description: row.product || "", quantity: row.qty || null, unit: row.unit || "",
            unit_price: row.unitPrice || null, amount: row.amount || null}))};
        state.candidate = copy(state.fields); renderEditor(); state.parseBaseline = JSON.stringify(collectFields());
        sourceStatus(`讀到 ${state.fields.rows.length} 筆；請逐欄核對，尚未保存。`);
      } else {
        state.matrix = await readXlsx(file);
        renderMapping(); sourceStatus(`讀到 ${state.matrix.length - 1} 列；先選欄位對照，尚未保存。`);
      }
    } catch (error) {
      sourceStatus(`${parseMessage(error.message)}。原檔和人工修改仍保留，可逐欄填寫後一起保存。`, true);
    }
  });
}
const mapKeys = ["company", "number", "date", "currency", ...rowKeys];
function renderMapping() {
  const target = $("mapping-fields"); target.replaceChildren();
  const headers = state.matrix[0];
  mapKeys.forEach(key => {const label = document.createElement("label"); label.textContent = labels[key];
    const select = document.createElement("select"); select.dataset.key = key; option(select, "", "留空／手動填寫");
    headers.forEach((name, index) => option(select, String(index), `${index + 1}. ${name || "未命名"}`));
    const guess = headers.findIndex(name => name.trim() === labels[key]);
    if (guess >= 0) select.value = String(guess);
    label.append(select); target.append(label);});
  $("mapping").hidden = false;
  const preview = $("xlsx-preview"); preview.replaceChildren();
  const table = document.createElement("table");
  for (const row of state.matrix.slice(0, 6)) {const tr = document.createElement("tr");
    for (const value of row) {const td = document.createElement("td"); td.textContent = value; tr.append(td);} table.append(tr);}
  preview.append(table);
}
function applyMapping() {
  if (!state.matrix) return;
  if (JSON.stringify(collectFields()) !== state.parseBaseline &&
      !window.confirm("重套 Excel 欄位對照會替換目前人工編修的欄位。確定繼續？")) return;
  const columns = Object.fromEntries([...$("mapping-fields").querySelectorAll("select")].map(select =>
    [select.dataset.key, select.value === "" ? null : Number(select.value)]));
  const data = state.matrix.slice(1).filter(row => row.some(value => String(value || "").trim()));
  if (!data.length || data.length > 100) return status("Excel 有效品項需為 1–100 列。", true);
  const take = (row, key) => columns[key] === null ? "" : String(row[columns[key]] || "").trim();
  const header = {};
  for (const key of ["company", "number", "date", "currency"]) {
    const values = [...new Set(data.map(row => take(row, key)).filter(Boolean))];
    header[key] = values.length === 1 ? values[0] : "";
    if (values.length > 1) status(`${labels[key]}欄有多個值；文件標題暫留空，請人工核對。`, true);
  }
  state.fields = {header, rows: data.map(row => ({id: crypto.randomUUID(),
    code: take(row, "code"), description: take(row, "description"),
    quantity: take(row, "quantity") || null, unit: take(row, "unit"),
    unit_price: take(row, "unit_price") || null, amount: take(row, "amount") || null}))};
  state.candidate = copy(state.fields); renderEditor(); state.parseBaseline = JSON.stringify(collectFields());
  sourceStatus(`已套用 ${data.length} 筆欄位對照；請逐欄核對，尚未保存。`);
}
async function hashFile(file) {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
}
async function filePayload(file) {
  if (!file) return null;
  const bytes = new Uint8Array(await file.arrayBuffer());
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const sha256 = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, "0")).join("");
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192)
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return {base64: btoa(binary), sha256};
}
async function saveDocument(approved = false) {
  if (activity.busy) return;
  const confirmed = collectFields(); const invalid = localValidate(confirmed);
  if (invalid) return status(invalid, true);
  const selected = state.selected && {id: state.selected.id, revision: state.selected.revision,
    file_sha256: state.selected.file_sha256};
  const draft = {kind: state.kind, source: state.source, candidate: copy(state.candidate),
    file: state.file, requestKey: state.requestKey};
  await activity.run(async () => {
    status("正在核對本機原檔；尚未送出。");
    try {
      const sourceFile = selected ? null : await filePayload(draft.file);
      const matches = suspectedDuplicates(state.documents, selected?.id, draft.kind, confirmed,
        sourceFile?.sha256 || selected?.file_sha256);
      const approval = JSON.stringify({kind: draft.kind, selectedId: selected?.id,
        confirmed, fileSha: sourceFile?.sha256 || selected?.file_sha256 || null,
        duplicateIds: matches.map(match => match.doc.id)});
      if (matches.length && (!approved || state.duplicateApproval !== approval)) {
        state.duplicateApproval = approval;
        $("duplicate-review-text").textContent = `找到疑似重複試用文件：${duplicateText(matches)}。請核對後決定是否仍要另外保存。`;
        $("duplicate-review").hidden = false;
        status("尚未送出；請核對疑似重複文件。", true);
        return;
      }
      clearDuplicateReview();
      status("正在保存人工確認欄位；原檔此時才會送至網站主機。");
      let saved;
      if (selected) saved = await api(`integration/documents/${selected.id}`, "PUT",
        {revision: selected.revision, confirmed});
      else saved = await api("integration/documents", "POST", {request_key: draft.requestKey,
        kind: draft.kind, source: draft.source, candidate: draft.candidate,
        confirmed, file: sourceFile}, 45000);
      state.selected = saved; state.file = null; state.fileSha = saved.file_sha256;
      state.fields = copy(saved.confirmed);
      state.candidate = saved.candidate; state.kind = saved.kind; state.source = saved.source;
      state.requestKey = crypto.randomUUID(); $("reload-document").hidden = true;
      renderEditor(); rememberBaseline(); status(`已保存；修訂 ${saved.revision}。`);
      try {await refresh();}
      catch {status(`已保存修訂 ${saved.revision}，但列表未能刷新；可重新載入查看。`, true);}
    } catch (error) {
      if (error.code === "TRIAL_VERSION_CONFLICT") $("reload-document").hidden = false;
      status(`保存失敗：${codeMessage(error.code)}${error.unknown ? "；結果未知，請用同一請求重試或重新載入查核。" : ""}`, true);
    }
  });
}
function openDocument(saved, force = false) {
  if (!saved) return;
  if (!force && !allowDiscard()) return;
  clearDuplicateReview();
  state.selected = saved; state.fields = copy(saved.confirmed); state.candidate = saved.candidate;
  state.source = saved.source; state.kind = saved.kind; state.file = null;
  state.fileSha = saved.file_sha256; state.matrix = null;
  $("mapping").hidden = true; $("source-file").value = ""; $("source-file").disabled = true;
  $("parse-file").disabled = true; $("reload-document").hidden = true;
  renderEditor(); rememberBaseline(); $("editor").scrollIntoView({block: "start"});
  sourceStatus("已開啟保存紀錄；解析候選值與人工確認值可分別檢查。");
}
async function openOriginal() {
  if (state.file) {const url = URL.createObjectURL(state.file); window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000); return;}
  if (!state.selected?.file_sha256) return;
  try {const response = await fetch(`${apiRoot}documents/${state.selected.id}/file`,
    {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error("FILE_UNAVAILABLE");
    const url = URL.createObjectURL(await response.blob()); window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch {status("原檔無法讀取；請確認登入與目前會話。", true);}
}
function queryString() {const params = new URLSearchParams();
  for (const key of ["company", "number", "code", "date"]) {
    const value = $("search-" + key).value.trim(); if (value) params.set(key, value);
  } return params.toString();}
async function search() {
  try {const response = await api(`integration/search?${queryString()}`);
    state.viewed = response.documents; renderDocuments(); renderStatistics(response);
  } catch (error) {status(`查詢失敗：${codeMessage(error.code)}`, true);}
}
function renderStatistics(data) {
  const parts = [];
  for (const kind of ["purchase_order", "invoice"])
    for (const [currency, amount] of Object.entries(data.totals_by_currency?.[kind] || {}))
      parts.push(`${kind === "invoice" ? "發票" : "採購單"} ${currency}：${amount}`);
  parts.push(`文件 ${data.documents.length} 筆`);
  if (data.missing_currency_documents) parts.push(`未填幣別 ${data.missing_currency_documents} 筆；不合計未知幣別金額`);
  if (data.missing_amount_documents) parts.push(`缺少金額 ${data.missing_amount_documents} 筆；統計不完整`);
  $("statistics").textContent = parts.join("\n");
}
function renderDocuments() {
  const target = $("document-list"); target.replaceChildren();
  if (!state.viewed.length) return target.textContent = "目前篩選沒有試用文件。";
  for (const doc of state.viewed) {const wrapper = document.createElement("div"); wrapper.className = "list-item";
    const button = document.createElement("button"); button.type = "button";
    const h = doc.confirmed.header;
    button.textContent = `${doc.kind === "invoice" ? "發票" : "採購單"} ${h.company || "未填公司"} · ${h.number || "未填單號"} · ${h.date || "未填日期"} · ${doc.confirmed.rows.length} 筆 · 修訂 ${doc.revision}`;
    button.addEventListener("click", () => openDocument(doc)); wrapper.append(button); target.append(wrapper);}
}
function renderLinks() {
  for (const [id, kind] of [["invoice-item", "invoice"], ["po-item", "purchase_order"]]) {
    const select = $(id); select.replaceChildren(); option(select, "", "請選文件品項");
    for (const doc of state.documents.filter(item => item.kind === kind))
      for (const item of doc.confirmed.rows) option(select, `${doc.id}:${item.id}`,
        `${doc.confirmed.header.number || "未填單號"} · ${item.code || item.description || "未填品項"} · ${item.quantity ?? "?"} ${item.unit}`);
  }
  const target = $("link-list"); target.replaceChildren();
  for (const link of state.links) {const line = document.createElement("div"); line.className = "list-item";
    const invoice = state.documents.find(doc => doc.id === link.invoice_document_id);
    const po = state.documents.find(doc => doc.id === link.po_document_id);
    const rowA = invoice?.confirmed.rows.find(row => row.id === link.invoice_item_id);
    const rowB = po?.confirmed.rows.find(row => row.id === link.po_item_id);
    const text = document.createElement("span");
    text.textContent = `${invoice?.confirmed.header.number || "發票"} ${rowA?.code || "未填品號"} → ${po?.confirmed.header.number || "採購單"} ${rowB?.code || "未填品號"}；分配 ${link.quantity ?? "未知"} `;
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "撤回";
    remove.addEventListener("click", async () => {try {await api(`integration/links/${link.id}`, "DELETE"); await refresh();
      status("已撤回對應；文件保留。 ");} catch (error) {status(`撤回失敗：${codeMessage(error.code)}`, true);}});
    line.append(text, remove); target.append(line);}
  renderComparison();
}
function renderComparison() {
  const target = $("comparison-list"); target.replaceChildren();
  const entries = summarizeItems(state.documents, state.links);
  if (!entries.length) {target.textContent = "尚無試用文件品項。"; return;}
  const table = document.createElement("table");
  const head = document.createElement("thead"); const heading = document.createElement("tr");
  for (const label of ["文件", "品項", "單位", "已配", "未配", "狀態"]) {
    const cell = document.createElement("th"); cell.textContent = label; heading.append(cell);
  }
  head.append(heading); table.append(head);
  const body = document.createElement("tbody");
  for (const entry of entries) {
    const row = document.createElement("tr");
    for (const value of [
      `${entry.kind === "invoice" ? "發票" : "採購單"} ${entry.number || "未填單號"}`,
      entry.code || entry.description || "未填品項", entry.unit || "未填",
      entry.allocated ?? "未知", entry.remaining ?? "未知", entry.status]) {
      const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
    }
    body.append(row);
  }
  table.append(body); target.append(table);
}
async function addLink() {
  const invoice = $("invoice-item").value.split(":"), po = $("po-item").value.split(":");
  if (invoice.length !== 2 || po.length !== 2) return status("請選發票與採購單品項。", true);
  try {await api("integration/links", "POST", {invoice_document_id: invoice[0], invoice_item_id: invoice[1],
    po_document_id: po[0], po_item_id: po[1], quantity: $("allocation").value.trim() || null});
  } catch (error) {return status(`對應失敗：${codeMessage(error.code)}`, true);}
  status("已建立可撤回的對應。 ");
  try {await refresh();} catch {status("對應已建立，但列表未能刷新；可重新載入。", true);}
}
function renderProducts() {
  const target = $("product-list"); target.replaceChildren();
  for (const product of state.products) {const line = document.createElement("div"); line.className = "list-item";
    const description = document.createElement("span");
    description.textContent = `${product.label} · 建議單位 ${product.unit || "未填"} · ` +
      product.aliases.map(alias => `${alias.company}: ${alias.code}`).join("、") + " ";
    const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "編輯主檔";
    edit.addEventListener("click", () => openProduct(product));
    line.append(description, edit); target.append(line);}
  if (!$("editor").hidden) state.fields.rows.forEach((_, index) => renderSuggestion(index));
}
function productDraft() {
  return {label: $("product-label").value.trim(), unit: $("product-unit").value.trim(),
    aliases: copy(state.productAliases).map(alias => ({company: alias.company.trim(), code: alias.code.trim()}))};
}
function rememberProductBaseline() {state.productBaseline = JSON.stringify(productDraft());}
function productHasUnsaved() {return JSON.stringify(productDraft()) !== state.productBaseline;}
function renderAliasRows() {
  const target = $("alias-rows"); target.replaceChildren();
  state.productAliases.forEach((alias, index) => {
    const line = document.createElement("div"); line.className = "row-card";
    const fields = document.createElement("div"); fields.className = "row-fields";
    fields.append(field(`公司 ${index + 1}`, alias.company, value => {alias.company = value;}, 120),
      field(`品號 ${index + 1}`, alias.code, value => {alias.code = value;}, 80));
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "移除此別名";
    remove.addEventListener("click", () => {if (state.productAliases.length <= 1)
      return status("主檔至少需要一組公司品號。", true);
    state.productAliases.splice(index, 1); renderAliasRows();});
    line.append(fields, remove); target.append(line);
  });
  $("add-alias").disabled = state.productAliases.length >= 20;
}
function newProduct(force = false) {
  if (!force && productHasUnsaved() && !window.confirm("主檔有未保存修改。確定放棄並新建？")) return;
  state.selectedProduct = null; state.productAliases = [{company: "", code: ""}];
  $("product-label").value = ""; $("product-unit").value = "";
  $("product-mode").textContent = "新增主檔"; $("save-product").textContent = "建立主檔";
  $("reload-product").hidden = true; renderAliasRows(); rememberProductBaseline();
}
function openProduct(product, force = false) {
  if (!force && productHasUnsaved() && !window.confirm("主檔有未保存修改。確定放棄並切換？")) return;
  state.selectedProduct = product; state.productAliases = copy(product.aliases);
  $("product-label").value = product.label; $("product-unit").value = product.unit;
  $("product-mode").textContent = `編修主檔；修訂 ${product.revision}`;
  $("save-product").textContent = "保存主檔修訂"; $("reload-product").hidden = true;
  renderAliasRows(); rememberProductBaseline(); $("product-mode").scrollIntoView({block: "start"});
}
async function saveProduct() {
  if (activity.busy) return;
  const draft = productDraft();
  if (!draft.label || !draft.aliases.length || draft.aliases.some(alias => !alias.company || !alias.code))
    return status("請填主檔描述與每組公司、品號；空白別名可先移除。", true);
  const current = state.selectedProduct && {id: state.selectedProduct.id,
    revision: state.selectedProduct.revision};
  await activity.run(async () => {
    status("正在保存主檔修訂。");
    try {
      const saved = current ? await api(`integration/products/${current.id}`, "PUT",
        {revision: current.revision, ...draft}) : await api("integration/products", "POST", draft);
      state.selectedProduct = saved; state.productAliases = copy(saved.aliases);
      $("product-mode").textContent = `編修主檔；修訂 ${saved.revision}`;
      $("save-product").textContent = "保存主檔修訂"; $("reload-product").hidden = true;
      renderAliasRows(); rememberProductBaseline(); status("主檔已保存；原文件欄位未改動。");
      try {await refresh();} catch {status("主檔已保存，但列表未能刷新；可重新載入。", true);}
    } catch (error) {
      if (error.code === "TRIAL_PRODUCT_VERSION_CONFLICT") $("reload-product").hidden = false;
      status(`主檔保存失敗：${codeMessage(error.code)}${error.unknown ? "；結果未知，請先重新載入查核，不要直接重試。" : ""}`, true);
    }
  });
}
async function exportCsv() {
  try {const response = await fetch(`${apiRoot}export.csv?${queryString()}`,
    {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) throw new Error("CSV_UNAVAILABLE");
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = "orderflow-trial.csv";
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
  } catch {status("CSV 匯出失敗；請確認登入。", true);}
}
async function refresh() {
  const data = await api("integration/bootstrap");
  $("workspace").hidden = false; $("login-section").hidden = true;
  state.documents = data.documents; state.links = data.links; state.products = data.products;
  renderLinks(); renderProducts(); updateDuplicateHint(); await search();
}
async function login(event) {
  event.preventDefault(); const password = $("password").value; $("password").value = "";
  try {await api("login", "POST", {password}); await refresh(); status("已登入；未存欄位仍保留在此頁。 ");}
  catch (error) {status(`登入失敗：${codeMessage(error.code)}`, true);}
}
$("login-form").addEventListener("submit", login);
$("source").addEventListener("change", chooseSource);
$("kind").addEventListener("change", () => {if (!state.selected) {
  clearDuplicateReview(); state.kind = $("kind").value; updateDuplicateHint();}});
$("editor").addEventListener("input", clearDuplicateReview);
for (const key of ["company", "number"]) $("field-" + key).addEventListener("input", updateDuplicateHint);
$("source-file").addEventListener("change", fileSelected);
$("parse-file").addEventListener("click", parseFile);
$("new-manual").addEventListener("click", () => newDocument());
$("open-original").addEventListener("click", openOriginal);
$("apply-mapping").addEventListener("click", applyMapping);
$("add-row").addEventListener("click", () => {if (state.fields.rows.length >= 100) return status("最多 100 筆品項。", true);
  state.fields.rows.push(blankRow()); renderRows();});
$("save-document").addEventListener("click", () => saveDocument());
$("confirm-duplicate").addEventListener("click", () => saveDocument(true));
$("cancel-duplicate").addEventListener("click", () => {
  clearDuplicateReview(); status("已取消保存；原檔與欄位仍留在瀏覽器，沒有送出。");
});
$("reload-document").addEventListener("click", async () => {try {const fresh = await api(`integration/documents/${state.selected.id}`);
  openDocument(fresh, true); status("已載入最新版；先前未存修改已放棄。 ");}
  catch (error) {status(`重載失敗：${codeMessage(error.code)}`, true);}});
$("search").addEventListener("click", search);
$("export").addEventListener("click", exportCsv);
$("add-link").addEventListener("click", addLink);
$("add-alias").addEventListener("click", () => {if (state.productAliases.length >= 20)
  return status("最多 20 組公司品號。", true);
  state.productAliases.push({company: "", code: ""}); renderAliasRows();});
$("save-product").addEventListener("click", saveProduct);
$("new-product").addEventListener("click", () => newProduct());
$("reload-product").addEventListener("click", async () => {
  try {const data = await api("integration/bootstrap");
    const fresh = data.products.find(product => product.id === state.selectedProduct?.id);
    if (!fresh) return status("找不到原主檔；畫面修改仍保留。", true);
    openProduct(fresh, true); state.products = data.products; renderProducts();
    status("已載入最新版主檔；先前未存修改已放棄。");
  } catch (error) {status(`重載主檔失敗：${codeMessage(error.code)}`, true);}
});
window.addEventListener("beforeunload", event => {if (activity.busy || hasUnsaved() || productHasUnsaved()) {
  event.preventDefault(); event.returnValue = "";
}});
newDocument(true); newProduct(true); refresh().catch(error => {$("login-section").hidden = false;
  status(error.status === 401 ? "請先登入網站。" : `載入失敗：${codeMessage(error.code)}`, error.status !== 401);});
