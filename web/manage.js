"use strict";
const apiBase = "/orderflow/api/";
const el = id => document.getElementById(id);
const state = {documents: [], jobs: [], drafts: [], recordSets: [], localSources: [],
  localSource: null, localPreview: null, parseTask: null, key: false, selected: null,
  job: null, rows: [], recordIssues: [], revision: 0, editSerial: 0,
  dirty: false, busy: false, uploadKey: null,
  uploadFile: null, pendingFile: null, pendingKind: null, pendingRecordEdit: null,
  pendingJobKeys: new Map(), page: "dashboard"};
let keyFieldUsed = false;
let keyWorkspaceShown = false;
function clearKeyInput() {
  const field = el("ai-key");
  field.value = "";
  field.readOnly = true;
  keyFieldUsed = false;
}
function prepareKeyInput() {
  clearKeyInput();
  const field = el("ai-key");
  field.addEventListener("focus", () => {
    if (field.readOnly) {
      field.value = "";
      field.readOnly = false;
    }
  });
  field.addEventListener("input", () => { keyFieldUsed = true; });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && !keyFieldUsed) clearKeyInput();
  });
}
function keyInputError(value) {
  if (!value) return "請先貼上 Google AI Studio API key；不要輸入網站密碼。";
  if (value.length > 256 || /[^\x21-\x7e]/.test(value) || value.includes("://") || /^www\./i.test(value))
    return "這看起來不是單一 API key。請勿貼網址、空白或網站密碼；請從 AI Studio 重新複製。";
  return null;
}
const uuid = () => crypto.randomUUID();
const diagnostics = window.OrderflowDiagnostics?.create(apiBase) || {
  record() {}, flush() {}, summary() { return "瀏覽器事件記錄未載入。"; },
  newOperation() { return uuid(); }, get traceId() { return ""; }
};
function status(id, message, kind = "") { el(id).textContent = message; el(id).dataset.state = kind; }
function safeError(error) {
  const parts = [error.code || "NETWORK_ERROR"];
  if (error.status) parts.push(`HTTP ${error.status}`);
  if (error.upstreamStatus) parts.push(`Google HTTP ${error.upstreamStatus}`);
  if (error.upstreamReason) parts.push(error.upstreamReason);
  if (error.appMarker === "MISSING") parts.push("未收到應用程式回應");
  if (error.responseType && error.responseType !== "JSON") parts.push(`回應類型 ${error.responseType}`);
  if (error.requestId) parts.push(`請求 ${error.requestId}`);
  return parts.join(" · ");
}
async function api(path, options = {}, timeoutMs = 10000) {
  const requestId = uuid();
  const started = Date.now();
  let appReached = false;
  let canReport = false;
  diagnostics.record("send", path, {requestId});
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(apiBase + path, { ...options, signal: controller.signal,
      credentials: "same-origin", cache: "no-store",
      headers: {...options.headers, "X-Orderflow-Request": "1",
        "X-Orderflow-Request-Id": requestId, "X-Orderflow-Trace-Id": diagnostics.traceId} });
    const appMarker = response.headers.get("X-Orderflow-Origin") === "app" ? "APP" : "MISSING";
    appReached = appMarker === "APP";
    canReport = appReached && response.status !== 401 && path !== "logout";
    const type = (response.headers.get("Content-Type") || "").split(";", 1)[0].toLowerCase();
    const responseType = type === "application/json" ? "JSON" :
      type === "text/html" ? "HTML" : type === "text/plain" ? "TEXT" : type ? "OTHER" : "MISSING";
    diagnostics.record("http_received", path, {requestId, httpStatus: response.status,
      responseType, durationMs: Date.now() - started});
    diagnostics.record("marker_checked", path, {requestId, marker: appMarker});
    if (appReached && response.headers.get("X-Orderflow-Request-Id") !== requestId)
      throw {code: "REQUEST_ID_MISMATCH", status: response.status, appMarker,
        responseType, requestId, unknown: true};
    let data;
    try { data = await response.json(); }
    catch { throw {code: "BAD_JSON_RESPONSE", status: response.status, appMarker,
      responseType, requestId}; }
    diagnostics.record("json_parsed", path, {requestId, httpStatus: response.status});
    if (!response.ok) {
      if (response.status === 401 && path !== "login") {
        const recordSave = path.startsWith("management/record-sets/");
        if (recordSave && !state.localPreview) state.pendingRecordEdit = {
          documentId: state.selected, jobId: state.job?.id, localSourceId: state.localSource?.id,
          revision: state.revision, rows: state.rows.map(row => ({...row}))};
        const preserve = recordSave || !!state.pendingRecordEdit || !!state.localPreview;
        showLogin(preserve ? "登入已過期；未存品項暫留在此頁。請重新登入後核對再保存。"
          : "登入已過期，請重新登入。", preserve);
      }
      throw {code: typeof data.error_code === "string" && /^[A-Z0-9_]{2,40}$/.test(data.error_code)
        ? data.error_code : "HTTP_ERROR", status: response.status, appMarker, responseType: "JSON", requestId,
        errors: Array.isArray(data.errors) ? data.errors : undefined,
        upstreamStatus: Number.isInteger(data.upstream_http_status) ? data.upstream_http_status : undefined,
        upstreamReason: ["INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNCLASSIFIED"].includes(data.upstream_reason)
          ? data.upstream_reason : undefined};
    }
    return data;
  } catch (error) {
    const failure = error?.name === "AbortError"
      ? {code: "REQUEST_TIMEOUT", requestId, unknown: true}
      : error?.code ? error : {code: "NETWORK_ERROR", requestId, unknown: true};
    diagnostics.record(failure.code === "BAD_JSON_RESPONSE" ? "json_failed" : "request_unknown",
      path, {requestId, code: failure.code, durationMs: Date.now() - started});
    throw failure;
  } finally { clearTimeout(timer); void diagnostics.flush(canReport); }
}
async function copyDiagnosticReport() {
  const field = el("diagnostic-report");
  field.value = diagnostics.summary();
  try {
    if (!navigator.clipboard?.writeText) throw Error("unavailable");
    await navigator.clipboard.writeText(field.value);
    status("diagnostic-copy-status", "已複製診斷資訊。", "good");
  } catch {
    const details = field.closest?.("details");
    if (details) details.open = true;
    field.focus(); field.select();
    status("diagnostic-copy-status", "無法自動複製；已選取內容，請按 Ctrl/Cmd+C。", "unknown");
  }
}
function showLogin(message = "請輸入網站密碼。", preserveEditor = false) {
  keyWorkspaceShown = false;
  el("startup").hidden = true; el("workspace").hidden = true; el("login").hidden = false;
  el("logout").hidden = true; el("password").value = ""; clearKeyInput();
  if (!preserveEditor) {
    state.documents = []; state.jobs = []; state.drafts = []; state.recordSets = [];
    state.localSources = []; state.localSource = null; state.localPreview = null;
    state.selected = null; state.job = null; state.rows = []; state.pendingRecordEdit = null;
  }
  state.pendingFile = null; state.pendingKind = null; state.key = false;
  state.pendingJobKeys.clear(); el("csv-export").hidden = true;
  el("retry-upload").hidden = true;
  clearJobErrorTools();
  status("login-status", message);
}
function showWorkspace(data) {
  state.documents = data.documents || []; state.jobs = data.jobs || [];
  state.drafts = data.drafts || []; state.recordSets = data.record_sets || [];
  state.localSources = data.local_sources || [];
  const pending = state.pendingRecordEdit;
  const canRestore = pending && state.documents.some(doc => doc.id === pending.documentId) &&
    (state.jobs.some(job => job.id === pending.jobId && job.document_id === pending.documentId && job.state === "done") ||
      state.localSources.some(source => source.id === pending.localSourceId && source.document_id === pending.documentId));
  if (canRestore) { state.selected = pending.documentId; state.job = null; }
  state.key = !!data.ai_key_configured;
  if (!keyWorkspaceShown) {
    clearKeyInput();
    keyWorkspaceShown = true;
  }
  el("startup").hidden = true; el("login").hidden = true; el("workspace").hidden = false;
  el("logout").hidden = false; el("csv-export").hidden = false;
  el("key-chip").textContent = state.key ? "已設定" : "未設定";
  el("key-chip").className = state.key ? "badge good" : "badge";
  status("key-status", state.key
    ? "金鑰已暫存，欄位不回填。輸入形式不代表有效；可至診斷測試頁明確執行文字連線檢查。"
    : "尚未設定金鑰。", state.key ? "good" : "");
  if (state.selected && !state.documents.some(doc => doc.id === state.selected)) state.selected = null;
  if (state.localPreview) state.selected = null;
  if (!state.selected && !state.localPreview && state.documents.length) state.selected = state.documents[0].id;
  renderDocuments();
  if (state.localPreview) activateLocalPreview(true);
  else if (canRestore && pending.localSourceId) selectLocalSource(pending.localSourceId);
  else if (canRestore) selectJob(pending.jobId);
  else if (state.localSource) selectLocalSource(state.localSource.id);
  else if (state.job) selectJob(state.job.id);
  else selectSelectedSource();
  if (canRestore) {
    state.rows = pending.rows.map(row => ({...row})); state.revision = pending.revision;
    state.dirty = true; state.pendingRecordEdit = null; editRecordRows();
    status("draft-status", "重新登入後已還原未存品項；請核對修訂，版本若已變更會拒絕覆蓋。", "unknown");
  } else if (pending) {
    state.pendingRecordEdit = null;
    status("draft-status", "重新登入後找不到原文件或辨識來源；沒有自動送出或覆蓋資料。", "error");
  }
  renderManagementViews();
  refreshControls();
}
async function load() {
  el("startup").hidden = false; el("retry").hidden = true;
  status("startup-status", "正在確認登入及資料狀態。");
  try { showWorkspace(await api("management/bootstrap")); }
  catch (error) {
    if (error.status === 401) {
      const preserve = !!(state.pendingRecordEdit || state.localPreview);
      showLogin(preserve ? "登入仍未完成；未存品項暫留在此頁。" : "請輸入網站密碼。", preserve);
      return;
    }
    status("startup-status", `載入失敗：${safeError(error)}。可重新載入。`, "error");
    el("retry").hidden = false;
  }
}
function refreshControls() {
  const editable = (state.job?.state === "done") || !!state.localSource || !!state.localPreview;
  el("recognize").disabled = !state.selected || !state.key || state.busy ||
    !!state.recordSets.find(set => set.document_id === state.selected && set.source_local_id);
  el("upload").disabled = !(state.pendingFile || el("pdf").files?.length) || state.busy;
  el("local-parse").disabled = !el("local-pdf").files?.length || state.busy;
  el("local-cancel").disabled = !state.parseTask;
  el("add-row").disabled = !editable || state.busy;
  el("save-draft").disabled = !editable || !state.rows.length || !state.dirty || state.busy;
  el("clear-key").disabled = !state.key || state.busy;
}
function renderDocuments() {
  const target = el("documents"); target.replaceChildren();
  if (!state.documents.length) { target.textContent = "尚無管理文件。診斷測試的上傳不會出現在這裡。"; return; }
  state.documents.forEach((doc, index) => {
    const button = document.createElement("button"); button.type = "button"; button.className = "list-button";
    button.setAttribute("aria-pressed", String(doc.id === state.selected));
    const label = doc.document_kind === "purchase_order" ? "採購單" : doc.document_kind === "invoice" ? "發票" : "未分類舊草稿";
    button.textContent = `${label} ${state.documents.length - index} · ${doc.page_count || "?"} 頁 · ${Math.ceil(doc.size / 1024)} KB · ${new Date(doc.created_ms).toLocaleString()}`;
    button.addEventListener("click", () => { if (state.busy) return;
      if (state.localPreview && !window.confirm("本機解析預覽尚未儲存。要放棄編修並選取其他文件嗎？")) return;
      state.selected = doc.id; state.job = null; renderDocuments(); renderJobs();
      state.localPreview = null; el("local-hints").hidden = true;
      selectSelectedSource(); refreshControls(); });
    target.append(button);
  });
  const selected = state.documents.find(doc => doc.id === state.selected);
  el("selection").textContent = selected ? `已選文件：${selected.page_count || "?"} 頁、${selected.size} bytes。`
    : state.localPreview ? "本機解析預覽：尚未上傳 PDF。" : "請先上傳或選取文件。";
  renderJobs();
}
function renderJobs() {
  const target = el("jobs"); target.replaceChildren();
  const relevant = state.jobs.filter(job => job.document_id === state.selected);
  if (!relevant.length) { target.textContent = "這份文件尚無辨識工作。"; return; }
  relevant.forEach((job, index) => {
    const button = document.createElement("button"); button.type = "button"; button.className = "list-button";
    button.setAttribute("aria-pressed", String(job.id === state.job?.id));
    button.textContent = `辨識 ${relevant.length - index} · ${job.state}${job.error_code ? ` · ${job.error_code}` : ""} · ${new Date(job.created_ms).toLocaleString()}`;
    button.addEventListener("click", () => { if (!state.busy) selectJob(job.id); });
    target.append(button);
  });
}
function clearDraft() {
  state.job = null; state.localSource = null; state.rows = []; state.revision = 0; state.dirty = false;
  el("rows").replaceChildren(); status("job-status", "尚未啟動。");
  clearJobErrorTools();
  el("draft-source").textContent = "讀取 PDF 或完成辨識後，這裡會顯示可核對的品項。";
  status("draft-status", "尚未儲存。未儲存的修改在重新整理後會消失。");
  renderJobs(); refreshControls();
}
function editRows(rows) {
  resetRecordIssues();
  state.rows = rows.map(row => ({description: row.description, quantity: row.quantity}));
  const target = el("rows"); target.replaceChildren();
  state.rows.forEach((row, index) => {
    const wrap = document.createElement("div"); wrap.className = "row-editor";
    const desc = document.createElement("label"); const descTitle = document.createElement("span");
    descTitle.textContent = `品項 ${index + 1}`; desc.append(descTitle);
    const descInput = document.createElement("input"); descInput.type = "text"; descInput.maxLength = 200;
    descInput.value = row.description; descInput.addEventListener("input", () => { state.rows[index].description = descInput.value; markDirty(); }); desc.append(descInput);
    const qty = document.createElement("label"); const qtyTitle = document.createElement("span");
    qtyTitle.textContent = "數量"; qty.append(qtyTitle);
    const qtyInput = document.createElement("input"); qtyInput.type = "number"; qtyInput.min = "0"; qtyInput.max = "1000000000"; qtyInput.step = "1";
    qtyInput.value = String(row.quantity); qtyInput.addEventListener("input", () => { state.rows[index].quantity = qtyInput.value; markDirty(); }); qty.append(qtyInput);
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "quiet"; remove.textContent = "移除";
    remove.addEventListener("click", () => { state.rows.splice(index, 1); editRows(state.rows); markDirty(); });
    wrap.append(desc, qty, remove); target.append(wrap);
  });
  refreshControls();
}
function markDirty() { state.editSerial++; state.dirty = true;
  status("draft-status", "有未儲存的編修；重新整理後會消失。", "unknown"); refreshControls(); }
function utcTime(value) {
  if (!Number.isSafeInteger(value) || value <= 0) return "unknown";
  const time = new Date(value);
  return Number.isFinite(time.getTime()) ? time.toISOString() : "unknown";
}
function jobErrorSummary(job) {
  const validUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const id = typeof job.id === "string" && validUuid.test(job.id) ? job.id : "unknown";
  const safeCodes = new Set(["AI_UNAVAILABLE", "AI_TIMEOUT_UNKNOWN", "AI_NOT_CONFIGURED",
    "AI_RATE_LIMITED", "AI_HTTP_ERROR", "AI_BAD_RESPONSE", "AI_AUTH_FAILED",
    "AI_MODEL_UNAVAILABLE", "AI_BAD_REQUEST", "AI_HTTP_UNKNOWN",
    "RESULT_FORMAT_INVALID", "INTERNAL_UNKNOWN"]);
  const code = safeCodes.has(job.error_code) ? job.error_code : "UNKNOWN";
  const created = utcTime(job.created_ms);
  const started = utcTime(job.started_ms);
  const finished = utcTime(job.finished_ms);
  const elapsed = created !== "unknown" && started !== "unknown" && finished !== "unknown" &&
    job.started_ms >= job.created_ms && job.finished_ms >= job.started_ms
    ? String(job.finished_ms - job.started_ms) : "unknown";
  const steps = job.steps || {};
  const upstreamStatus = Number.isInteger(steps.upstream_http_status) &&
    steps.upstream_http_status >= 100 && steps.upstream_http_status <= 599
    ? String(steps.upstream_http_status) : "unknown";
  const upstreamReason = ["INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNCLASSIFIED"]
    .includes(steps.upstream_reason) ? steps.upstream_reason : "unknown";
  const transportClass = ["DNS", "TLS", "CONNECT_REFUSED", "TIMEOUT_UNKNOWN",
    "IO_UNKNOWN", "HTTP_TRUNCATED", "HTTP_MALFORMED"].includes(steps.transport_class)
    ? steps.transport_class : "unknown";
  return ["OrderFlow 辨識錯誤資訊", `job_id: ${id}`,
    `state: ${job.state === "failed" ? "failed" : "unknown"}`, `error_code: ${code}`,
    `created_utc: ${created}`, `started_utc: ${started}`, `finished_utc: ${finished}`,
    `elapsed_ms: ${elapsed}`, `upstream_http_status: ${upstreamStatus}`,
    `upstream_reason: ${upstreamReason}`, `transport_class: ${transportClass}`].join("\n");
}
function clearJobErrorTools() {
  el("job-error-tools").hidden = true;
  el("job-error-details").value = "";
  status("job-error-copy-status", "");
}
function renderJobErrorTools(job) {
  clearJobErrorTools();
  if (job.state !== "failed" && job.state !== "unknown") return;
  el("job-error-details").value = jobErrorSummary(job);
  el("job-error-tools").hidden = false;
}
async function copyJobError() {
  const selected = state.job;
  if (el("job-error-tools").hidden || !selected ||
      (selected.state !== "failed" && selected.state !== "unknown")) return;
  const details = el("job-error-details");
  const text = jobErrorSummary(selected);
  details.value = text;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(text);
    if (state.job === selected) status("job-error-copy-status", "已複製此筆錯誤資訊。", "good");
  } catch {
    if (state.job !== selected) return;
    details.focus(); details.select();
    status("job-error-copy-status", "無法自動複製；已選取摘要，請按 Ctrl/Cmd+C 手動複製。", "unknown");
  }
}
function selectJob(id) {
  const job = state.jobs.find(item => item.id === id && item.document_id === state.selected);
  if (!job) { clearDraft(); return; }
  state.job = job; state.localSource = null; state.localPreview = null;
  renderJobs(); renderJobErrorTools(job);
  diagnostics.record("job_rendered", `management/jobs/${id}`);
  void diagnostics.flush(true);
  if (job.state === "queued" || job.state === "running") { status("job-status", "辨識執行中…");
    clearDraftEditor(); pollJob(id); return; }
  if (job.state !== "done") { status("job-status",
    job.state === "unknown"
      ? "辨識結果不明。請求可能已送達 Google 並計費；請勿連續按辨識。稍後重新載入，若仍不清楚請複製下方資訊。"
      : "辨識未完成。請查看下方錯誤資訊並聯絡提供網站的人；系統不會自動重試。",
    job.state === "unknown" ? "unknown" : "error");
    clearDraftEditor(); return; }
  status("job-status", "辨識完成。請逐項核對，必要時編修並儲存草稿。", "good");
  const selectedDoc = state.documents.find(doc => doc.id === state.selected);
  if (selectedDoc?.document_kind === "purchase_order" || selectedDoc?.document_kind === "invoice") {
    selectTypedJob(job, selectedDoc.document_kind);
    return;
  }
  el("save-draft").textContent = "儲存草稿";
  const saved = state.drafts.find(draft => draft.source_job_id === id);
  state.revision = saved?.revision || 0; state.dirty = !saved;
  el("draft-source").textContent = `來源辨識 ${id}；${saved ? `已儲存修訂 ${saved.revision}` : "尚未儲存"}。`;
  const rows = saved?.rows || job.result || [];
  editRows(rows.length ? rows : [{description: "", quantity: 0}]);
  status("draft-status", saved ? `已儲存修訂 ${saved.revision}。` : "請核對後按「儲存草稿」。未儲存的編修會消失。", saved ? "good" : "");
  refreshControls();
}
function clearDraftEditor() {
  resetRecordIssues();
  state.rows = []; state.revision = 0; state.dirty = false; el("rows").replaceChildren();
  el("draft-source").textContent = "只有成功的管理辨識工作可建立草稿。";
  status("draft-status", "尚無可編修的草稿。"); refreshControls();
}
async function pollJob(id) {
  if (state.polling === id) return;
  state.polling = id;
  try {
    for (let attempt = 0; attempt < 20 && state.job?.id === id; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      const job = await api(`management/jobs/${id}`);
      state.jobs = state.jobs.map(item => item.id === id ? job : item);
      if (job.state !== "queued" && job.state !== "running") { selectJob(id); return; }
    }
    if (state.job?.id === id) status("job-status", "仍在等待；可稍後重新載入工作狀態。請勿直接重複送出。", "unknown");
  } catch (error) { if (state.job?.id === id) status("job-status", `查詢中斷：${safeError(error)}。可重新載入查詢，不會自動重送。`, "unknown"); }
  finally { state.polling = null; }
}
async function login(event) {
  event.preventDefault(); const password = el("password").value; el("password").value = "";
  el("login-button").disabled = true; status("login-status", "登入中…");
  try { await api("login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password})}); await load(); }
  catch (error) { status("login-status", `登入失敗：${safeError(error)}`, "error"); }
  finally { el("login-button").disabled = false; }
}
const localErrorMessages = {
  LOCAL_FILE_SIZE: "PDF 須大於 0 且不超過 8 MB。",
  LOCAL_PAGE_COUNT_UNSUPPORTED: "目前只支援單頁採購憑單。",
  LOCAL_PAGE_SIZE_UNSUPPORTED: "頁面尺寸與支援版型不符。",
  LOCAL_TEXT_UNAVAILABLE: "找不到可用文字層；目前不支援掃描圖或 OCR。",
  LOCAL_TEMPLATE_UNSUPPORTED: "此 PDF 不是目前支援的科雅採購憑單版型。",
  LOCAL_LAYOUT_UNSUPPORTED: "欄位位置或格式與支援版型不同，請人工檢查。",
  LOCAL_ROWS_UNSUPPORTED: "品項列不完整或有重複，請人工檢查。",
  LOCAL_NUMBER_UNSUPPORTED: "數字格式不受支援，請人工檢查。",
  LOCAL_DATE_UNSUPPORTED: "日期格式或日期值不受支援，請人工檢查。",
  LOCAL_ROW_TOTAL_MISMATCH: "品項數量、單價與金額不一致，未產生候選。",
  LOCAL_DOCUMENT_TOTAL_MISMATCH: "表尾合計與品項不一致，未產生候選。",
  LOCAL_PDF_UNREADABLE: "PDF 無法在瀏覽器讀取，請檢查檔案。",
  LOCAL_TIMEOUT: "本機解析逾時，已停止處理。",
  LOCAL_CANCELLED: "已取消本機解析。",
};
function selectSelectedSource() {
  const saved = state.recordSets.find(item => item.document_id === state.selected);
  const local = state.localSources.find(item => item.document_id === state.selected);
  if (saved?.source_local_id && local) { selectLocalSource(local.id); return; }
  if (!saved && local) { selectLocalSource(local.id); return; }
  const job = state.jobs.find(item => item.id === saved?.source_job_id)
    || state.jobs.find(item => item.document_id === state.selected);
  if (job) selectJob(job.id);
  else clearDraft();
}
function selectLocalSource(id) {
  const source = state.localSources.find(item => item.id === id && item.document_id === state.selected);
  if (!source) { clearDraft(); return; }
  const saved = state.recordSets.find(item => item.document_id === state.selected && item.source_local_id === id);
  state.localPreview = null; state.localSource = source; state.job = null;
  state.rows = (saved?.rows || source.candidate_rows).map(row => ({...row}));
  state.revision = saved?.revision || 0; state.dirty = !saved;
  clearJobErrorTools(); renderJobs();
  el("draft-source").textContent = `本機解析 ${source.parser_id} · 來源 ${id}；${saved ? `已儲存修訂 ${saved.revision}` : "尚未確認保存"}。`;
  el("save-draft").textContent = saved ? "儲存修訂" : "確認並儲存記錄";
  editRecordRows();
  status("draft-status", saved ? `已儲存修訂 ${saved.revision}。`
    : "請逐欄核對本機解析候選後保存；PDF 已存於網站主機。", saved ? "good" : "unknown");
  refreshControls();
}
function activateLocalPreview(preserveRows = false) {
  const preview = state.localPreview;
  if (!preview) return;
  state.selected = null; state.job = null; state.localSource = null;
  if (!preserveRows) state.rows = preview.candidateRows.map(row => ({...row}));
  state.revision = 0; state.dirty = true;
  clearJobErrorTools(); renderDocuments();
  const hints = preview.hints;
  el("local-hints").hidden = false;
  el("local-hints").textContent = `來源提示：廠商 ${hints.supplier}；備註 ${hints.remark || "無"}；預進貨日 ${hints.expectedArrivals.join("、")}；數量合計 ${hints.quantityTotal}（${hints.missingUnits} 筆單位空白）；金額合計 ${hints.documentTotal}。備註及預進貨日不會被當作訂單日期或貿易條件。`;
  el("draft-source").textContent = `已讀到 ${preview.candidateRows.length} 筆；請核對，尚未儲存。`;
  el("save-draft").textContent = "確認並儲存記錄";
  editRecordRows();
  status("draft-status", "請核對每欄，尤其空白單位；確認後 PDF 與人工記錄才會送到網站主機保存。", "unknown");
  showPage("upload");
}
function cancelLocalParse() { state.parseTask?.cancel(); }
async function parseLocal() {
  const file = el("local-pdf").files?.[0];
  if (!file || state.busy) return;
  if (!file.name.toLowerCase().endsWith(".pdf") || file.size < 1 || file.size > 8 * 1024 * 1024) {
    status("local-status", localErrorMessages.LOCAL_FILE_SIZE, "error"); return;
  }
  if (state.localPreview?.file === file) {
    status("local-status", "這份 PDF 已有本機預覽；請繼續核對欄位。", "unknown"); return;
  }
  if (state.localPreview && !window.confirm("目前本機預覽尚未儲存。要放棄編修並解析另一份 PDF 嗎？")) return;
  if (state.localPreview) { state.localPreview = null; clearDraft(); el("local-hints").hidden = true; renderDocuments(); }
  const task = {cancelled: false, cancel() { this.cancelled = true; this.reject?.({code: "LOCAL_CANCELLED"}); }};
  state.parseTask = task; state.busy = true; refreshControls();
  status("local-status", "正在此瀏覽器解析文字層；PDF 尚未上傳…");
  try {
    const bytes = await file.arrayBuffer();
    if (task.cancelled) throw {code: "LOCAL_CANCELLED"};
    const worker = new Worker("/orderflow/local-pdf-worker.mjs", {type: "module"});
    task.worker = worker;
    const result = await new Promise((resolve, reject) => {
      task.reject = reject;
      const timer = setTimeout(() => reject({code: "LOCAL_TIMEOUT"}), 15000);
      worker.onmessage = event => {
        if (typeof event.data?.ok !== "boolean") return;
        if (event.data.ok) resolve(event.data.result);
        else reject({code: event.data.code || "LOCAL_PDF_UNREADABLE"});
      };
      worker.onerror = () => reject({code: "LOCAL_PDF_UNREADABLE"});
      task.cleanup = () => clearTimeout(timer);
      worker.postMessage({bytes}, [bytes]);
    });
    if (task.cancelled) throw {code: "LOCAL_CANCELLED"};
    state.localPreview = {file, parserId: result.parserId, kind: result.kind,
      candidateRows: result.rows.map(row => makeRecordRow(row, result.kind)), hints: result.hints,
      uploadKey: uuid(), sourceKey: uuid()};
    activateLocalPreview();
    status("local-status", `已讀到 ${result.rows.length} 筆。請核對後儲存；目前尚未儲存。`, "good");
  } catch (error) {
    status("local-status", localErrorMessages[error?.code] || localErrorMessages.LOCAL_PDF_UNREADABLE,
      error?.code === "LOCAL_CANCELLED" ? "unknown" : "error");
  } finally {
    task.cleanup?.(); task.worker?.terminate();
    state.parseTask = null; state.busy = false; refreshControls();
  }
}
async function saveLocalPreview(rows) {
  const preview = state.localPreview;
  if (!preview || state.busy) return;
  const serial = state.editSerial;
  diagnostics.newOperation();
  state.busy = true; refreshControls();
  status("draft-status", "正在上傳 PDF 並保存人工確認記錄…");
  try {
    const bytes = await preview.file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha = Array.from(new Uint8Array(digest), part => part.toString(16).padStart(2, "0")).join("");
    const doc = await api("management/documents", {method: "POST", body: bytes,
      headers: {"Content-Type": "application/pdf", "X-File-Size": String(bytes.byteLength),
        "X-File-SHA256": sha, "X-Request-Key": preview.uploadKey,
        "X-Document-Kind": "purchase_order"}}, 30000);
    preview.documentId = doc.id;
    const source = await api("management/local-sources", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({document_id: doc.id,
        request_key: preview.sourceKey, parser_id: preview.parserId,
        candidate_rows: preview.candidateRows})});
    const current = state.recordSets.find(set => set.document_id === doc.id);
    const record = await api(`management/record-sets/${doc.id}`, {method: "PUT",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({source_local_id: source.id,
        revision: current?.revision || 0, rows})});
    state.documents = [doc, ...state.documents.filter(item => item.id !== doc.id)];
    state.localSources = [source, ...state.localSources.filter(item => item.id !== source.id)];
    state.recordSets = [record, ...state.recordSets.filter(item => item.document_id !== doc.id)];
    state.localPreview = null; state.localSource = source; state.selected = doc.id;
    state.revision = record.revision;
    if (state.editSerial === serial) { state.rows = record.rows.map(row => ({...row})); state.dirty = false; }
    else state.dirty = true;
    renderDocuments(); editRecordRows(); renderManagementViews();
    status("draft-status", state.dirty ? "送出時的版本已保存；後續編修仍在欄位中，請再次核對保存。"
      : `PDF 與人工確認記錄已存於網站主機，修訂 ${record.revision}；未送 Google。`, state.dirty ? "unknown" : "good");
    if (!state.dirty) showPage("orders");
  } catch (error) {
    const issues = error.code === "RECORD_ROWS_INVALID" ? recordIssuesFromApi(error.errors) : [];
    if (issues.length) { state.recordIssues = issues; renderRecordIssues(issues, true); }
    status("draft-status", `保存未確認：${recordError(error)}；本機編修仍保留。請核對文件列表後再決定是否重試。`, "unknown");
  } finally { state.busy = false; refreshControls(); }
}

async function upload() {
  const file = state.pendingFile || el("pdf").files?.[0]; if (!file || state.busy) return;
  const documentKind = state.pendingKind;
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    status("upload-status", "請選擇 PDF 檔案。", "error"); return;
  }
  if (file.size < 1 || file.size > 8 * 1024 * 1024) { status("upload-status", "PDF 須大於 0 且不超過 8 MB。", "error"); return; }
  state.busy = true; refreshControls(); status("upload-status", "正在檢查並上傳…");
  el("processing").hidden = false; el("processing-text").textContent = "正在檢查並上傳 PDF…";
  try {
    const bytes = await file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
    if (state.uploadFile !== file) { state.uploadFile = file; state.uploadKey = uuid(); }
    const doc = await api("management/documents", {method: "POST", body: bytes,
      headers: {"Content-Type": "application/pdf", "X-File-Size": String(bytes.byteLength),
        "X-File-SHA256": sha, "X-Request-Key": state.uploadKey,
        ...(documentKind ? {"X-Document-Kind": documentKind} : {})}}, 30000);
    state.documents = [doc, ...state.documents.filter(item => item.id !== doc.id)];
    state.selected = doc.id; state.job = null; el("pdf").value = "";
    el("retry-upload").hidden = true;
    state.pendingFile = null; state.pendingKind = null; state.uploadFile = null; state.uploadKey = null;
    el("purchase-pdf").value = ""; el("invoice-pdf").value = "";
    renderDocuments(); clearDraft();
    status("upload-status", doc.duplicate ? "這份 PDF 已在工作區，已選取原文件；不會重複辨識。"
      : doc.same_pdf_other_kind ? "上傳完成，但相同 PDF 曾以另一類型上傳；請核對分類後再啟動辨識。"
      : "上傳完成；請設定金鑰後啟動辨識。", doc.same_pdf_other_kind ? "unknown" : "good");
    showPage("upload");
  } catch (error) {
    status("upload-status", `上傳未確認：${safeError(error)}。先重新載入列表確認；重試同一份上傳會使用相同請求識別。`, "unknown");
    el("retry-upload").hidden = !state.pendingFile;
  }
  finally { state.busy = false; el("processing").hidden = true; refreshControls(); }
}
async function setKey() {
  const key = el("ai-key").value;
  const issue = keyInputError(key);
  if (issue) { status("key-status", issue, "error"); return; }
  try { await api("key", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key})});
    state.key = true; el("key-chip").textContent = "已設定"; el("key-chip").className = "badge good";
    status("key-status", "金鑰已暫存，15 分鐘後失效。格式不代表有效；可至診斷測試頁明確執行文字連線檢查。", "good"); }
  catch (error) { status("key-status", error.code === "BAD_KEY"
    ? "輸入形式不符。請從 AI Studio 複製單一金鑰，勿貼網站密碼或網址。"
    : `設定失敗：${safeError(error)}`, "error"); }
  finally { clearKeyInput(); refreshControls(); }
}
async function clearKey() {
  try { await api("key", {method: "DELETE"}); state.key = false;
    el("key-chip").textContent = "未設定"; el("key-chip").className = "badge";
    status("key-status", "金鑰已清除。"); }
  catch (error) { status("key-status", `清除未確認：${safeError(error)}`, "unknown"); }
  refreshControls();
}
async function recognize() {
  if (!state.selected || !state.key || state.busy) return;
  if (!window.confirm("將這份 PDF 傳給 Google 辨識，可能使用你的 API 額度。確定送出？")) return;
  diagnostics.newOperation();
  state.busy = true; refreshControls(); status("job-status", "正在建立辨識工作…");
  const documentId = state.selected;
  const requestKey = state.pendingJobKeys.get(documentId) || uuid();
  state.pendingJobKeys.set(documentId, requestKey);
  try {
    const job = await api("management/jobs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_id: documentId, request_key: requestKey, scenario: "real"})});
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)]; state.job = job;
    state.pendingJobKeys.delete(documentId); selectJob(job.id);
  } catch (error) { status("job-status", `送出狀態未確認：${safeError(error)}。先重新載入工作列表；不會自動重送付費請求。`, "unknown"); }
  finally { state.busy = false; refreshControls(); }
}
function collectRows() {
  if (state.rows.length < 1 || state.rows.length > 100) throw Error("請保留 1–100 列。");
  return state.rows.map(row => {
    const description = row.description.trim(); const quantity = Number(row.quantity);
    if (!description || description.length > 200 || !/^\d+$/.test(String(row.quantity)) || !Number.isSafeInteger(quantity) || quantity > 1_000_000_000)
      throw Error("每列須有品項名稱，數量須為 0 至 1,000,000,000 的整數。");
    return {description, quantity};
  });
}
async function saveDraft() {
  if (selectedDocumentKind()) return saveRecordSet();
  if (!state.job || state.job.state !== "done" || state.busy) return;
  let rows; try { rows = collectRows(); } catch (error) { status("draft-status", error.message, "error"); return; }
  state.busy = true; refreshControls(); status("draft-status", "正在儲存…");
  try {
    const draft = await api(`management/drafts/${state.job.id}`, {method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({rows, revision: state.revision})});
    state.drafts = [draft, ...state.drafts.filter(item => item.source_job_id !== draft.source_job_id)];
    state.revision = draft.revision; state.dirty = false; editRows(draft.rows);
    el("draft-source").textContent = `來源辨識 ${draft.source_job_id}；已儲存修訂 ${draft.revision}。`;
    status("draft-status", `草稿已儲存，修訂 ${draft.revision}。`, "good");
  } catch (error) { status("draft-status", `儲存未完成：${safeError(error)}。若為版本衝突，先重新載入核對。`, "error"); }
  finally { state.busy = false; refreshControls(); }
}
function selectedDocumentKind() {
  return state.localPreview?.kind || state.documents.find(doc => doc.id === state.selected)?.document_kind || null;
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char =>
    ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[char]);
}
function display(value) { return value === null || value === undefined || value === "" ? "—" : escapeHtml(value); }
function decimalUnits(value) {
  if (value === null || value === undefined || !/^(?:0|[1-9]\d{0,11})(?:\.\d{1,4})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  return BigInt(whole) * 10000n + BigInt(fraction.padEnd(4, "0"));
}
function formatUnits(value) {
  const negative = value < 0n;
  const magnitude = negative ? -value : value;
  const whole = (magnitude / 10000n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = (magnitude % 10000n).toString().padStart(4, "0").replace(/0+$/, "");
  return `${negative ? "−" : ""}${whole}${fraction ? "." + fraction : ""}`;
}
function formatDecimal(value) {
  const units = decimalUnits(value);
  return units === null ? "—" : formatUnits(units);
}
function liveRecords(kind) {
  return state.recordSets.filter(set => set.kind === kind).flatMap(set =>
    set.rows.filter(row => !row.deleted).map(row => ({row, set})));
}
function moneyGroups(records) {
  const sums = new Map(); let incomplete = 0;
  for (const {row} of records) {
    const amount = decimalUnits(row.amount);
    if (amount === null || !row.currency) { incomplete++; continue; }
    sums.set(row.currency, (sums.get(row.currency) || 0n) + amount);
  }
  return {sums, incomplete};
}
function moneySummary(records) {
  const {sums, incomplete} = moneyGroups(records);
  const parts = [...sums].sort(([a], [b]) => a.localeCompare(b)).map(([currency, sum]) =>
    `${formatUnits(sum)} ${currency}`);
  if (incomplete) parts.push(`${incomplete} 筆缺金額／幣別`);
  return parts.join(" · ") || "—";
}
function rowDateForPeriod(row, period, now = new Date()) {
  if (period === "all") return true;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(row.date || "")) return false;
  const year = Number(row.date.slice(0, 4));
  const month = Number(row.date.slice(5, 7));
  return year === now.getFullYear() && (period === "month"
    ? month === now.getMonth() + 1
    : Math.floor((month - 1) / 3) === Math.floor(now.getMonth() / 3));
}
function emptyRow(columns, message) {
  return `<tr><td colspan="${columns}" class="empty">${escapeHtml(message)}</td></tr>`;
}
function renderManagementViews() {
  const orders = liveRecords("purchase_order");
  const invoices = liveRecords("invoice");
  el("metric-orders").textContent = moneySummary(orders);
  el("metric-orders-count").textContent = `${orders.length} 筆`;
  const invoicedQty = invoices.map(({row}) => decimalUnits(row.qty));
  el("metric-invoice-qty").textContent = invoicedQty.length && invoicedQty.every(value => value !== null)
    && invoices.every(({row}) => row.unit && row.unit === invoices[0].row.unit)
    ? `${formatUnits(invoicedQty.reduce((a, b) => a + b, 0n))} ${invoices[0].row.unit}` : "—";
  el("metric-invoice-count").textContent = `${invoices.length} 筆；混合／未知單位不合計`;
  el("metric-invoices").textContent = moneySummary(invoices);
  el("metric-compared").textContent = String(comparisonData(orders, invoices).rows
    .filter(item => item.comparisonStatus === "人工連結").length);
  el("last-update").textContent = `檢視時間：${new Date().toLocaleTimeString("zh-TW")}`;
  el("recent-orders").innerHTML = orders.length ? orders.slice(0, 5).map(({row}) =>
    `<tr><td>${display(row.client)}</td><td>${display(row.product)}</td><td class="num">${formatDecimal(row.amount)} ${display(row.currency)}</td><td>${display(row.status)}</td></tr>`).join("")
    : emptyRow(4, "尚無已確認訂單");
  el("recent-invoices").innerHTML = invoices.length ? invoices.slice(0, 5).map(({row}) =>
    `<tr><td>${display(row.client)}</td><td>${display(row.product)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${formatDecimal(row.amount)} ${display(row.currency)}</td></tr>`).join("")
    : emptyRow(4, "尚無已確認發票");
  renderOrders(orders); renderInvoices(invoices); renderSales(invoices); renderComparison(orders, invoices);
}
function renderOrders(orders = liveRecords("purchase_order")) {
  const query = (el("order-search").value || "").toLocaleLowerCase();
  const statusFilter = el("order-status-filter").value;
  const status = {pending: "確認中", confirmed: "確定"}[statusFilter] || statusFilter;
  const rows = orders.filter(({row}) =>
    (!query || [row.client, row.product].some(value => (value || "").toLocaleLowerCase().includes(query)))
    && (!statusFilter || row.status === status));
  el("order-count").textContent = `${rows.length} 筆`;
  el("order-rows").innerHTML = rows.length ? rows.map(({row, set}) =>
    `<tr><td>${display(row.orderNo)}</td><td>${display(row.client)}</td><td>${display(row.product)}</td><td>${display(row.code)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${formatDecimal(row.amount)}</td><td>${display(row.currency)}</td><td>${display(row.date)}</td><td>${display(row.status)}</td><td><button class="table-delete quiet" type="button" data-doc="${set.document_id}" data-row="${row.id}" data-kind="purchase_order" aria-label="刪除訂單品項">刪除</button></td></tr>`).join("")
    : emptyRow(10, "無符合訂單");
}
function renderInvoices(invoices = liveRecords("invoice")) {
  const query = (el("invoice-search").value || "").toLocaleLowerCase();
  const rows = invoices.filter(({row}) => !query ||
    [row.client, row.product].some(value => (value || "").toLocaleLowerCase().includes(query)));
  el("invoice-count").textContent = `${rows.length} 筆`;
  el("invoice-rows").innerHTML = rows.length ? rows.map(({row, set}) =>
    `<tr><td>${display(row.invoiceNo)}</td><td>${display(row.client)}</td><td>${display(row.product)}</td><td>${display(row.code)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${formatDecimal(row.unitPrice)}</td><td class="num">${formatDecimal(row.amount)}</td><td>${display(row.currency)}</td><td>${display(row.date)}</td><td><button class="table-delete quiet" type="button" data-doc="${set.document_id}" data-row="${row.id}" data-kind="invoice" aria-label="刪除發票品項">刪除</button></td></tr>`).join("")
    : emptyRow(10, "無符合發票");
}
function renderSales(invoices = liveRecords("invoice")) {
  const query = (el("sales-search").value || "").toLocaleLowerCase();
  const period = el("sales-period").value || "all";
  const rows = invoices.filter(({row}) => rowDateForPeriod(row, period)
    && (!query || (row.client || "").toLocaleLowerCase().includes(query)));
  el("sales-rows").innerHTML = rows.length ? rows.map(({row}) =>
    `<tr><td>${display(row.date)}</td><td>${display(row.invoiceNo)}</td><td>${display(row.client)}</td><td>${display(row.product)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${formatDecimal(row.amount)}</td><td>${display(row.currency)}</td></tr>`).join("")
    : emptyRow(7, "無符合資料");
  el("sales-total").textContent = `合計：${moneySummary(rows)}`;
}
function comparisonData(orders = liveRecords("purchase_order"), invoices = liveRecords("invoice")) {
  const byOrder = new Map(orders.map(item => [item.row.id, item]));
  const quantities = new Map();
  const linkedCount = new Map();
  const unknown = new Set();
  const unmatched = [];
  for (const invoice of invoices) {
    const link = invoice.row.linked_order_row_id;
    if (!link || !byOrder.has(link)) { unmatched.push(invoice); continue; }
    const order = byOrder.get(link).row;
    linkedCount.set(link, (linkedCount.get(link) || 0) + 1);
    const qty = decimalUnits(invoice.row.qty);
    if (qty === null || !invoice.row.unit || !order.unit || invoice.row.unit !== order.unit) {
      unknown.add(link); continue;
    }
    quantities.set(link, (quantities.get(link) || 0n) + qty);
  }
  return {rows: orders.map(item => {
    const ordered = decimalUnits(item.row.qty);
    const billed = quantities.get(item.row.id) || 0n;
    const comparable = ordered !== null && item.row.unit && !unknown.has(item.row.id);
    return {...item, billed: comparable ? billed : null,
      difference: comparable ? ordered - billed : null,
      comparisonStatus: !linkedCount.has(item.row.id) ? "尚無連結發票"
        : comparable ? "人工連結" : "數量／單位待核對"};
  }), unmatched};
}
function renderComparison(orders = liveRecords("purchase_order"), invoices = liveRecords("invoice")) {
  const result = comparisonData(orders, invoices);
  el("recent-comparison").innerHTML = result.rows.length ? result.rows.slice(0, 5).map(({row, billed, difference, comparisonStatus}) =>
    `<tr><td>${display(row.product)}／${display(row.code)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${billed === null ? "—" : formatUnits(billed)}</td><td class="num">${difference === null ? "—" : formatUnits(difference)}</td><td>${comparisonStatus}</td></tr>`).join("")
    : emptyRow(5, "尚無可對照資料");
  const rows = result.rows.map(({row, billed, difference, comparisonStatus}) =>
    `<tr><td>${display(row.product)}</td><td>${display(row.code)}</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">${billed === null ? "—" : formatUnits(billed)}</td><td class="num">${difference === null ? "—" : formatUnits(difference)}</td><td>${comparisonStatus}</td></tr>`);
  rows.push(...result.unmatched.map(({row}) =>
    `<tr><td>${display(row.product)}</td><td>${display(row.code)}</td><td class="num">—</td><td class="num">${formatDecimal(row.qty)} ${display(row.unit)}</td><td class="num">—</td><td>未對應發票，待人工核對</td></tr>`));
  el("comparison-rows").innerHTML = rows.length ? rows.join("") : emptyRow(6, "尚無可對照資料");
}

function makeRecordRow(raw, kind) {
  const row = {id: uuid(), orderNo: null, invoiceNo: null, client: null, product: null,
    code: null, qty: null, unitPrice: null, amount: null, currency: null,
    date: null, incoterms: null, unit: null, status: kind === "purchase_order" ? "確認中" : null,
    linked_order_row_id: null, deleted: false};
  for (const key of ["orderNo", "invoiceNo", "client", "product", "code", "qty",
    "unitPrice", "amount", "currency", "date", "incoterms", "unit"]) {
    if (raw && typeof raw[key] === "string" && raw[key] !== "") row[key] = raw[key];
  }
  if (kind === "purchase_order") row.invoiceNo = null;
  else row.orderNo = null;
  return row;
}
function linkIssue(invoice, order) {
  if (!order) return "採購單品項不存在；請重新載入。";
  const fields = {client: "客戶", code: "品號", unit: "數量單位"};
  for (const [key, label] of Object.entries(fields)) {
    if (invoice[key] && order[key] && invoice[key] !== order[key])
      return `${label}與採購單不同，不能連結。`;
  }
  return null;
}
function selectTypedJob(job, kind) {
  const saved = state.recordSets.find(item => item.document_id === state.selected);
  if (saved?.source_local_id) {
    selectLocalSource(saved.source_local_id); return;
  }
  if (saved && saved.source_job_id !== job.id) {
    const source = state.jobs.find(item => item.id === saved.source_job_id);
    if (source) { selectJob(source.id); return; }
  }
  state.revision = saved?.revision || 0;
  state.dirty = !saved;
  state.rows = saved ? saved.rows.map(row => ({...row})) :
    (job.result || []).map(row => makeRecordRow(row, kind));
  if (!state.rows.length) state.rows = [makeRecordRow(null, kind)];
  el("draft-source").textContent = `來源辨識 ${job.id}；${saved ? `已儲存修訂 ${saved.revision}` : "尚未確認保存"}。`;
  el("save-draft").textContent = saved ? "儲存修訂" : "確認並儲存記錄";
  editRecordRows();
  status("draft-status", saved ? `已儲存修訂 ${saved.revision}。`
    : "請逐欄核對 AI 建議值後明確保存；未保存編修會消失。", saved ? "good" : "");
  refreshControls();
}
const recordLabels = {orderNo: "採購單號", invoiceNo: "發票號碼", client: "客戶", product: "品名",
  code: "品號", qty: "數量", unit: "數量單位", unitPrice: "單價", amount: "金額",
  currency: "幣別", date: "日期", incoterms: "貿易條件", status: "人工狀態",
  linked_order_row_id: "人工連結採購單品項", id: "品項識別", deleted: "刪除狀態",
  row: "品項", rows: "品項數"};
const recordTextLimits = {orderNo: 100, invoiceNo: 100, client: 200, product: 200,
  code: 100, currency: 3, date: 10, incoterms: 60, unit: 32};
const recordFieldNodes = new Map();
const recordErrorCodes = new Set(["ROW_COUNT", "ROW_FORMAT", "ROW_ID_DUPLICATE", "BOOLEAN_REQUIRED",
  "TEXT_INVALID", "TEXT_TOO_LONG", "CURRENCY_FORMAT", "DATE_FORMAT", "DATE_INVALID",
  "DECIMAL_FORMAT", "POSITIVE_REQUIRED", "FIELD_NOT_ALLOWED", "STATUS_INVALID",
  "ROW_ID_INVALID", "PRODUCT_OR_CODE_REQUIRED"]);
function recordIssueReason(issue) {
  const reasons = {
    ROW_COUNT: "請保留 1–100 筆品項。", ROW_FORMAT: "品項結構不正確，請重新載入後核對。",
    ROW_ID_DUPLICATE: "品項識別重複，請重新載入後核對。",
    BOOLEAN_REQUIRED: "刪除狀態不正確，請重新載入後核對。",
    TEXT_INVALID: "請輸入非空白文字；未知時請留空。",
    TEXT_TOO_LONG: `最多 ${recordTextLimits[issue.field] || 100} 字，請縮短內容。`,
    CURRENCY_FORMAT: "請輸入 3 個大寫英文字母，例如 USD；未知請留空。",
    DATE_FORMAT: "請輸入 YYYY-MM-DD 或 YYYY/M/D，例如 2026/9/22；不接受月/日/年。",
    DATE_INVALID: "日期不存在，請核對年月日；例如 2024/2/29。",
    DECIMAL_FORMAT: "請輸入最多 12 位整數、4 位小數的非負十進位數，例如 12.50；未知請留空。",
    POSITIVE_REQUIRED: "數量須大於 0，例如 1；未知請留空。",
    FIELD_NOT_ALLOWED: "這類文件不允許此欄位，請重新載入後核對。",
    STATUS_INVALID: "請選擇「確認中」或「確定」。",
    ROW_ID_INVALID: "連結的品項識別不正確，請重新選擇。",
    PRODUCT_OR_CODE_REQUIRED: "品名與品號至少填一欄，例如品名「螺絲」。",
  };
  return reasons[issue.code] || "請檢查這個欄位。";
}
function normalizeRecordDate(value) {
  if (value === null) return null;
  const iso = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/.exec(value);
  const slash = /^([0-9]{4})\/([0-9]{1,2})\/([0-9]{1,2})$/.exec(value);
  const parts = iso || slash;
  if (!parts) throw "DATE_FORMAT";
  const year = Number(parts[1]), month = Number(parts[2]), day = Number(parts[3]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [0, 31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month]) throw "DATE_INVALID";
  return iso ? value : `${parts[1]}-${parts[2].padStart(2, "0")}-${parts[3].padStart(2, "0")}`;
}
function collectRecordRows() {
  const issues = [];
  const add = (row, index, field, code) => {
    if (issues.length < 20) issues.push({row_id: row?.id || null, index, field, code});
  };
  if (state.rows.length < 1 || state.rows.length > 100)
    return {rows: [], issues: [{row_id: null, index: null, field: "rows", code: "ROW_COUNT"}]};
  const ids = new Set();
  const kind = selectedDocumentKind();
  const validRowId = value => typeof value === "string" &&
    /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(value);
  const rows = state.rows.map((original, offset) => {
    const index = offset + 1, row = {...original};
    if (!validRowId(row.id)) add(row, index, "row", "ROW_FORMAT");
    else if (ids.has(row.id)) add(row, index, "id", "ROW_ID_DUPLICATE");
    ids.add(row.id);
    if (typeof row.deleted !== "boolean") add(row, index, "deleted", "BOOLEAN_REQUIRED");
    for (const [field, limit] of Object.entries(recordTextLimits)) {
      const value = row[field];
      if (value === null) continue;
      if (typeof value !== "string" || !value.trim() || value.includes("\0")) add(row, index, field, "TEXT_INVALID");
      else if (value.length > limit) add(row, index, field, "TEXT_TOO_LONG");
    }
    if (typeof row.currency === "string" && row.currency.trim() && row.currency.length <= 3 && !/^[A-Z]{3}$/.test(row.currency))
      add(row, index, "currency", "CURRENCY_FORMAT");
    if (typeof row.date === "string" && row.date.trim() && row.date.length <= 10) {
      try { row.date = normalizeRecordDate(row.date); }
      catch (code) { add(row, index, "date", code); }
    }
    for (const field of ["qty", "unitPrice", "amount"]) {
      const value = row[field];
      if (value === null) continue;
      if (typeof value !== "string" || !/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$/.test(value))
        add(row, index, field, "DECIMAL_FORMAT");
      else if (field === "qty" && Number(value) <= 0) add(row, index, field, "POSITIVE_REQUIRED");
    }
    if (!row.deleted && row.product === null && row.code === null)
      add(row, index, "product", "PRODUCT_OR_CODE_REQUIRED");
    if (kind === "purchase_order") {
      if (row.invoiceNo !== null) add(row, index, "invoiceNo", "FIELD_NOT_ALLOWED");
      if (row.linked_order_row_id !== null) add(row, index, "linked_order_row_id", "FIELD_NOT_ALLOWED");
      if (!["確認中", "確定"].includes(row.status)) add(row, index, "status", "STATUS_INVALID");
    } else {
      if (row.orderNo !== null) add(row, index, "orderNo", "FIELD_NOT_ALLOWED");
      if (row.status !== null) add(row, index, "status", "FIELD_NOT_ALLOWED");
      if (row.linked_order_row_id !== null && !validRowId(row.linked_order_row_id))
        add(row, index, "linked_order_row_id", "ROW_ID_INVALID");
    }
    return row;
  });
  return {rows, issues};
}
function recordIssuesFromApi(errors) {
  if (!Array.isArray(errors)) return [];
  return errors.slice(0, 20).filter(issue => issue && Number.isInteger(issue.index) &&
    issue.index >= 1 && issue.index <= state.rows.length &&
    Object.hasOwn(recordLabels, issue.field) && recordErrorCodes.has(issue.code))
    .map(issue => ({index: issue.index, row_id: state.rows[issue.index - 1].id,
      field: issue.field, code: issue.code}));
}
function renderRecordIssues(issues, focus = false) {
  const summary = el("record-errors");
  for (const {input, hint} of recordFieldNodes.values()) {
    input.setAttribute("aria-invalid", "false"); input.setAttribute("aria-describedby", "");
    hint.textContent = ""; hint.hidden = true;
  }
  summary.replaceChildren(); summary.hidden = !issues.length;
  if (!issues.length) return;
  const title = document.createElement("p");
  title.textContent = `請修正以下 ${issues.length} 處後再保存；目前輸入仍保留。`;
  summary.append(title);
  const list = document.createElement("ul");
  issues.forEach(issue => {
    const label = issue.index ? `第 ${issue.index} 筆 · ${recordLabels[issue.field] || "品項"}` : "品項數";
    const message = `${label}：${recordIssueReason(issue)}`;
    const item = document.createElement("li");
    const node = recordFieldNodes.get(`${issue.row_id}|${issue.field}`);
    if (node) {
      node.input.setAttribute("aria-invalid", "true");
      node.input.setAttribute("aria-describedby", node.hint.id);
      node.hint.textContent = recordIssueReason(issue); node.hint.hidden = false;
      const button = document.createElement("button"); button.type = "button";
      button.className = "record-error-link"; button.textContent = message;
      button.addEventListener("click", () => node.input.focus()); item.append(button);
    } else item.textContent = message;
    list.append(item);
  });
  summary.append(list);
  if (focus) { summary.focus({preventScroll: true}); summary.scrollIntoView({block: "nearest"}); }
}
function clearRecordIssue(rowId, field) {
  const issues = state.recordIssues.filter(issue => !(issue.row_id === rowId && issue.field === field));
  if (issues.length !== state.recordIssues.length) {
    state.recordIssues = issues; renderRecordIssues(issues);
  }
}
function resetRecordIssues() {
  recordFieldNodes.clear(); state.recordIssues = []; renderRecordIssues([]);
}
function editRecordRows() {
  const kind = selectedDocumentKind();
  const target = el("rows"); target.replaceChildren();
  resetRecordIssues();
  const labels = {...recordLabels, date: "日期（YYYY-MM-DD 或 YYYY/M/D）", currency: "幣別（3 大寫字母）"};
  const fields = kind === "purchase_order"
    ? ["orderNo", "client", "product", "code", "qty", "unit", "unitPrice", "amount", "currency", "date", "incoterms"]
    : ["invoiceNo", "client", "product", "code", "qty", "unit", "unitPrice", "amount", "currency", "date", "incoterms"];
  state.rows.forEach((row, index) => {
    if (row.deleted) return;
    const wrap = document.createElement("div"); wrap.className = "record-editor";
    const heading = document.createElement("h3"); heading.textContent = `品項 ${index + 1}`; wrap.append(heading);
    const grid = document.createElement("div"); grid.className = "record-grid";
    fields.forEach(field => {
      const label = document.createElement("label"); label.textContent = labels[field];
      const input = document.createElement("input"); input.type = "text";
      input.value = row[field] ?? ""; input.maxLength = recordTextLimits[field] || 17;
      input.addEventListener("input", () => { row[field] = input.value === "" ? null : input.value;
        clearRecordIssue(row.id, field); markDirty(); });
      const hint = document.createElement("span"); hint.id = `record-error-${row.id}-${field}`;
      hint.className = "record-field-error"; hint.hidden = true;
      recordFieldNodes.set(`${row.id}|${field}`, {input, hint});
      label.append(input, hint); grid.append(label);
    });
    if (kind === "purchase_order") {
      const label = document.createElement("label"); label.textContent = "人工狀態";
      const select = document.createElement("select");
      for (const statusValue of ["確認中", "確定"]) {
        const option = document.createElement("option"); option.value = statusValue;
        option.textContent = statusValue; select.append(option);
      }
      select.value = row.status || "確認中";
      select.addEventListener("change", () => { row.status = select.value;
        clearRecordIssue(row.id, "status"); markDirty(); });
      const hint = document.createElement("span"); hint.id = `record-error-${row.id}-status`;
      hint.className = "record-field-error"; hint.hidden = true;
      recordFieldNodes.set(`${row.id}|status`, {input: select, hint});
      label.append(select, hint); grid.append(label);
    } else {
      const label = document.createElement("label"); label.textContent = "人工連結採購單品項";
      const select = document.createElement("select");
      const none = document.createElement("option"); none.value = ""; none.textContent = "未對應"; select.append(none);
      liveRecords("purchase_order").forEach(({row: order}) => {
        const option = document.createElement("option"); option.value = order.id;
        option.textContent = [order.orderNo, order.client, order.product, order.code]
          .filter(Boolean).join(" / ") || order.id;
        select.append(option);
      });
      select.value = row.linked_order_row_id || "";
      select.addEventListener("change", () => {
        const linked = select.value ? liveRecords("purchase_order")
          .find(item => item.row.id === select.value)?.row : null;
        const issue = select.value ? linkIssue(row, linked) : null;
        if (issue) { select.value = row.linked_order_row_id || ""; showToast(issue, "error"); return; }
        row.linked_order_row_id = select.value || null;
        clearRecordIssue(row.id, "linked_order_row_id"); markDirty();
        if (linked && ["client", "code", "unit"].some(key => !row[key] || !linked[key]))
          showToast("連結欄位有缺值；請人工核對客戶、品號與數量單位。", "");
      });
      const hint = document.createElement("span"); hint.id = `record-error-${row.id}-linked_order_row_id`;
      hint.className = "record-field-error"; hint.hidden = true;
      recordFieldNodes.set(`${row.id}|linked_order_row_id`, {input: select, hint});
      label.append(select, hint); grid.append(label);
      const note = document.createElement("p"); note.className = "muted";
      note.textContent = "只有人工連結且數量單位可比的發票品項會進訂購／開票數量對照。";
      wrap.append(note);
    }
    wrap.append(grid);
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "quiet";
    remove.textContent = "刪除此品項";
    remove.addEventListener("click", () => {
      if (!window.confirm("確定從列表與統計移除此品項？原 PDF 與辨識結果仍會保留。")) return;
      row.deleted = true; editRecordRows(); markDirty();
    });
    wrap.append(remove); target.append(wrap);
  });
  refreshControls();
}
function recordError(error) {
  const messages = {
    RECORD_VERSION_CONFLICT: "其他頁面已更新此文件；請重新載入後核對修訂。",
    RECORD_LINK_CONFLICT: "客戶、品號或數量單位與所連結採購單不同；請修正或取消連結。",
    RECORD_LINK_NOT_FOUND: "連結的採購單品項不存在或已刪除；請重新選擇。",
    RECORD_LINKED_ROW: "此採購單品項仍被發票連結；請先在發票取消連結。",
    RECORD_ROWS_INVALID: "品項資料有誤；請核對各欄提示。若未顯示欄位提示，請重新載入後核對。",
    RECORD_SOURCE_NOT_FOUND: "來源不可用；需同一登入工作區的成功管理辨識或本機解析來源。",
    LOCAL_SOURCE_CONFLICT: "這份文件已有不同的本機解析來源；請重新載入核對。",
    LOCAL_DOCUMENT_NOT_FOUND: "文件不存在或不是支援的單頁採購憑單。",
    RECORD_ROW_ID_CONFLICT: "記錄品項識別與其他文件衝突，請重新載入。",
  };
  return messages[error.code] || safeError(error);
}
async function saveRecordSet() {
  if (!(state.job?.state === "done" || state.localSource || state.localPreview) || state.busy) return;
  const checked = collectRecordRows();
  if (checked.issues.length) {
    state.recordIssues = checked.issues; renderRecordIssues(checked.issues, true);
    status("draft-status", `保存前找到 ${checked.issues.length} 處欄位問題；輸入未清除。`, "error");
    return;
  }
  state.recordIssues = []; renderRecordIssues([]);
  if (state.localPreview) return saveLocalPreview(checked.rows);
  const submittedSerial = state.editSerial;
  state.busy = true; refreshControls(); status("draft-status", "正在保存人工確認的記錄…");
  try {
    const source = state.localSource ? {source_local_id: state.localSource.id} : {source_job_id: state.job.id};
    const record = await api(`management/record-sets/${state.selected}`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...source, revision: state.revision, rows: checked.rows}),
    });
    state.recordSets = [record, ...state.recordSets.filter(item => item.document_id !== record.document_id)];
    state.revision = record.revision;
    if (state.editSerial !== submittedSerial) {
      state.dirty = true; renderManagementViews();
      status("draft-status", `已保存送出時的修訂 ${record.revision}；送出後的新編修仍在欄位中，請再核對並保存。`, "unknown");
      return;
    }
    state.rows = record.rows.map(row => ({...row})); state.dirty = false;
    editRecordRows(); renderManagementViews();
    status("draft-status", `已保存修訂 ${record.revision}；原 PDF 與${state.localSource ? "本機候選" : "辨識結果"}仍保留。`, "good");
    showPage(record.kind === "purchase_order" ? "orders" : "invoices");
    showToast("人工確認記錄已保存", "good");
  } catch (error) {
    const issues = error.code === "RECORD_ROWS_INVALID" ? recordIssuesFromApi(error.errors) : [];
    if (issues.length) { state.recordIssues = issues; renderRecordIssues(issues, true); }
    status("draft-status", `保存失敗：${recordError(error)}；未存輸入仍保留。`, "error");
  } finally { state.busy = false; refreshControls(); }
}
async function deleteRecordRow(documentId, rowId, kind) {
  const set = state.recordSets.find(item => item.document_id === documentId && item.kind === kind);
  if (!set || state.busy) return;
  if (!window.confirm("確定從列表與統計刪除此品項？原 PDF、辨識結果與刪除記錄會保留。")) return;
  const rows = set.rows.map(row => row.id === rowId ? {...row, deleted: true} : {...row});
  state.busy = true; refreshControls();
  try {
    const updated = await api(`management/record-sets/${documentId}`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...(set.source_local_id ? {source_local_id: set.source_local_id}
        : {source_job_id: set.source_job_id}), revision: set.revision, rows}),
    });
    state.recordSets = [updated, ...state.recordSets.filter(item => item.document_id !== documentId)];
    if (state.selected === documentId &&
        (state.job?.id === updated.source_job_id || state.localSource?.id === updated.source_local_id)) {
      state.rows = updated.rows.map(row => ({...row})); state.revision = updated.revision;
      state.dirty = false; editRecordRows();
    }
    renderManagementViews(); showToast("品項已從列表與統計移除", "good");
  } catch (error) { showToast(`刪除未完成：${recordError(error)}`, "error"); }
  finally { state.busy = false; refreshControls(); }
}
function csvCell(value) {
  let cell = String(value ?? "");
  if (/^\s*[=+\-@]/u.test(cell)) cell = "'" + cell;
  return '"' + cell.replace(/"/g, '""') + '"';
}
function csvText() {
  const rows = [["類型", "編號", "客戶", "品名", "品號", "數量", "數量單位", "單價", "金額",
    "幣別", "日期", "貿易條件", "狀態", "採購單品項連結", "來源文件 ID", "記錄品項 ID"]];
  for (const kind of ["purchase_order", "invoice"]) {
    for (const {row, set} of liveRecords(kind)) {
      rows.push([kind === "purchase_order" ? "訂單" : "發票",
        kind === "purchase_order" ? row.orderNo : row.invoiceNo,
        row.client, row.product, row.code, row.qty, row.unit, row.unitPrice,
        row.amount, row.currency, row.date, row.incoterms, row.status,
        row.linked_order_row_id, set.document_id, row.id]);
    }
  }
  return "\uFEFF" + rows.map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
}
function exportCsv() {
  const blob = new Blob([csvText()], {type: "text/csv;charset=utf-8"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url;
  link.download = `物流文件_${new Date().toISOString().slice(0, 10)}.csv`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 60000);
}
let toastTimer = null;
function showToast(message, kind = "") {
  const target = el("toast"); target.textContent = message;
  target.style.background = kind === "good" ? "#166534" : kind === "error" ? "#991b1b" : "#18170f";
  target.className = "toast show";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { target.className = "toast"; }, 4000);
}
function showPage(name) {
  state.page = name;
  if (typeof document.querySelectorAll !== "function") return;
  document.querySelectorAll(".page").forEach(page => {
    const active = page.id === `page-${name}`;
    page.hidden = !active; page.classList.toggle("active", active);
  });
  document.querySelectorAll(".nav-item").forEach(button => {
    const active = button.dataset.page === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}
function chooseTypedFile(file, kind) {
  if (!file) return;
  state.pendingFile = file; state.pendingKind = kind;
  state.uploadFile = null; state.uploadKey = null; el("retry-upload").hidden = true;
  showPage("upload"); upload();
}

prepareKeyInput();
el("login-form").addEventListener("submit", login);
el("retry").addEventListener("click", load);
el("logout").addEventListener("click", async () => {
  try { await api("logout", {method: "POST"}); showLogin("已登出。"); }
  catch (error) { status("key-status", `登出未確認：${safeError(error)}。請重試。`, "unknown"); }
});
el("pdf").addEventListener("change", () => { state.uploadFile = null; state.uploadKey = null; refreshControls(); });
el("upload").addEventListener("click", upload);
el("local-pdf").addEventListener("change", () => refreshControls());
el("local-parse").addEventListener("click", parseLocal);
el("local-cancel").addEventListener("click", cancelLocalParse);
el("retry-upload").addEventListener("click", upload);
el("set-key").addEventListener("click", setKey);
el("clear-key").addEventListener("click", clearKey);
el("recognize").addEventListener("click", recognize);
el("copy-job-error").addEventListener("click", copyJobError);
el("copy-diagnostic").addEventListener("click", copyDiagnosticReport);
el("add-row").addEventListener("click", () => {
  if (state.rows.length >= 100) return;
  const kind = selectedDocumentKind();
  if (kind) { state.rows.push(makeRecordRow(null, kind)); editRecordRows(); }
  else editRows([...state.rows, {description: "", quantity: 0}]);
  markDirty();
});
el("save-draft").addEventListener("click", saveDraft);
el("csv-export").addEventListener("click", exportCsv);
for (const [id, kind] of [["purchase-pdf", "purchase_order"], ["invoice-pdf", "invoice"]]) {
  el(id).addEventListener("change", event => chooseTypedFile(event.target.files?.[0], kind));
}
for (const id of ["order-search", "order-status-filter"]) {
  el(id).addEventListener(id === "order-search" ? "input" : "change", () => renderOrders());
}
el("invoice-search").addEventListener("input", () => renderInvoices());
el("sales-search").addEventListener("input", () => renderSales());
el("sales-period").addEventListener("change", () => renderSales());
for (const id of ["order-rows", "invoice-rows"]) {
  el(id).addEventListener("click", event => {
    const button = event.target.closest(".table-delete");
    if (button) deleteRecordRow(button.dataset.doc, button.dataset.row, button.dataset.kind);
  });
}
if (typeof document.querySelectorAll === "function") {
  document.querySelectorAll(".nav-item").forEach(button =>
    button.addEventListener("click", () => showPage(button.dataset.page)));
  for (const [id, kind] of [["purchase-pdf", "purchase_order"], ["invoice-pdf", "invoice"]]) {
    const box = document.querySelector(`label[for="${id}"]`);
    box.addEventListener("click", event => { event.preventDefault(); el(id).click(); });
    box.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault(); el(id).click();
      }
    });
    box.addEventListener("dragover", event => { event.preventDefault(); box.classList.add("drag"); });
    box.addEventListener("dragleave", () => box.classList.remove("drag"));
    box.addEventListener("drop", event => {
      event.preventDefault(); box.classList.remove("drag");
      const file = event.dataTransfer.files?.[0];
      if (!file || !file.name.toLowerCase().endsWith(".pdf")) showToast("請選擇 PDF 檔案", "error");
      else chooseTypedFile(file, kind);
    });
  }
}
load();
