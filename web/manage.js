"use strict";
const apiBase = "/orderflow/api/";
const el = id => document.getElementById(id);
const state = {documents: [], jobs: [], drafts: [], key: false, selected: null,
  job: null, rows: [], revision: 0, dirty: false, busy: false, uploadKey: null,
  uploadFile: null, jobKey: null};
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
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(apiBase + path, { ...options, signal: controller.signal,
      credentials: "same-origin", cache: "no-store",
      headers: {...options.headers, "X-Orderflow-Request": "1", "X-Orderflow-Request-Id": requestId} });
    const appMarker = response.headers.get("X-Orderflow-Origin") === "app" ? "APP" : "MISSING";
    const type = (response.headers.get("Content-Type") || "").split(";", 1)[0].toLowerCase();
    let data;
    try { data = await response.json(); }
    catch { throw {code: "BAD_JSON_RESPONSE", status: response.status, appMarker,
      responseType: type === "text/html" ? "HTML" : type || "MISSING", requestId}; }
    if (!response.ok) {
      if (response.status === 401 && path !== "login") showLogin("登入已過期，請重新登入。");
      throw {code: typeof data.error_code === "string" && /^[A-Z0-9_]{2,40}$/.test(data.error_code)
        ? data.error_code : "HTTP_ERROR", status: response.status, appMarker, responseType: "JSON", requestId,
        upstreamStatus: Number.isInteger(data.upstream_http_status) ? data.upstream_http_status : undefined,
        upstreamReason: ["INVALID_ARGUMENT", "FAILED_PRECONDITION", "UNCLASSIFIED"].includes(data.upstream_reason)
          ? data.upstream_reason : undefined};
    }
    return data;
  } catch (error) {
    if (error?.name === "AbortError") throw {code: "REQUEST_TIMEOUT", requestId, unknown: true};
    if (error?.code) throw error;
    throw {code: "NETWORK_ERROR", requestId, unknown: true};
  } finally { clearTimeout(timer); }
}
function showLogin(message = "請輸入網站密碼。") {
  keyWorkspaceShown = false;
  el("startup").hidden = true; el("workspace").hidden = true; el("login").hidden = false;
  el("logout").hidden = true; el("password").value = ""; clearKeyInput();
  state.documents = []; state.jobs = []; state.drafts = []; state.selected = null; state.job = null;
  state.rows = []; state.key = false; status("login-status", message);
}
function showWorkspace(data) {
  state.documents = data.documents || []; state.jobs = data.jobs || []; state.drafts = data.drafts || [];
  state.key = !!data.ai_key_configured;
  if (!keyWorkspaceShown) {
    clearKeyInput();
    keyWorkspaceShown = true;
  }
  el("startup").hidden = true; el("login").hidden = true; el("workspace").hidden = false;
  el("logout").hidden = false;
  status("key-status", state.key
    ? "金鑰已暫存，欄位不回填。輸入形式不代表有效；可至診斷測試頁明確執行文字連線檢查。"
    : "尚未設定金鑰。", state.key ? "good" : "");
  if (state.selected && !state.documents.some(doc => doc.id === state.selected)) state.selected = null;
  if (!state.selected && state.documents.length) state.selected = state.documents[0].id;
  renderDocuments();
  if (state.job) selectJob(state.job.id);
  else {
    const latest = state.jobs.find(job => job.document_id === state.selected);
    if (latest) selectJob(latest.id); else clearDraft();
  }
  refreshControls();
}
async function load() {
  el("startup").hidden = false; el("retry").hidden = true;
  status("startup-status", "正在確認登入及資料狀態。");
  try { showWorkspace(await api("management/bootstrap")); }
  catch (error) {
    if (error.status === 401) { showLogin(); return; }
    status("startup-status", `載入失敗：${safeError(error)}。可重新載入。`, "error");
    el("retry").hidden = false;
  }
}
function refreshControls() {
  el("recognize").disabled = !state.selected || !state.key || state.busy;
  el("upload").disabled = !el("pdf").files?.length || state.busy;
  el("add-row").disabled = !state.job || state.job.state !== "done" || state.busy;
  el("save-draft").disabled = !state.job || state.job.state !== "done" || !state.rows.length || !state.dirty || state.busy;
  el("clear-key").disabled = !state.key || state.busy;
}
function renderDocuments() {
  const target = el("documents"); target.replaceChildren();
  if (!state.documents.length) { target.textContent = "尚無管理文件。診斷測試的上傳不會出現在這裡。"; return; }
  state.documents.forEach((doc, index) => {
    const button = document.createElement("button"); button.type = "button"; button.className = "list-button";
    button.setAttribute("aria-pressed", String(doc.id === state.selected));
    button.textContent = `文件 ${state.documents.length - index} · ${doc.page_count || "?"} 頁 · ${Math.ceil(doc.size / 1024)} KB · ${new Date(doc.created_ms).toLocaleString()}`;
    button.addEventListener("click", () => { state.selected = doc.id; state.job = null; renderDocuments(); renderJobs();
      const latest = state.jobs.find(job => job.document_id === doc.id);
      if (latest) selectJob(latest.id); else clearDraft(); refreshControls(); });
    target.append(button);
  });
  const selected = state.documents.find(doc => doc.id === state.selected);
  el("selection").textContent = selected ? `已選文件：${selected.page_count || "?"} 頁、${selected.size} bytes。` : "請先上傳或選取文件。";
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
    button.addEventListener("click", () => selectJob(job.id)); target.append(button);
  });
}
function clearDraft() {
  state.job = null; state.rows = []; state.revision = 0; state.dirty = false;
  el("rows").replaceChildren(); status("job-status", "尚未啟動。");
  el("draft-source").textContent = "完成真正辨識後，才會在這裡顯示可編修的品項。";
  status("draft-status", "尚無管理草稿。未儲存的編修在重新整理後會消失。");
  renderJobs(); refreshControls();
}
function editRows(rows) {
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
function markDirty() { state.dirty = true; status("draft-status", "有未儲存的編修；重新整理後會消失。", "unknown"); refreshControls(); }
function selectJob(id) {
  const job = state.jobs.find(item => item.id === id && item.document_id === state.selected);
  if (!job) { clearDraft(); return; }
  state.job = job; renderJobs();
  if (job.state === "queued" || job.state === "running") { status("job-status", "辨識執行中…");
    clearDraftEditor(); pollJob(id); return; }
  if (job.state !== "done") { status("job-status", `辨識${job.state === "unknown" ? "結果不明" : "失敗"}：${job.error_code || "UNKNOWN"}${job.steps?.upstream_http_status ? ` · Google HTTP ${job.steps.upstream_http_status}` : ""}${job.steps?.upstream_reason ? ` · ${job.steps.upstream_reason}` : ""}。不會自動重試。`, job.state === "unknown" ? "unknown" : "error");
    clearDraftEditor(); return; }
  status("job-status", "辨識完成。請逐項核對，必要時編修並儲存草稿。", "good");
  const saved = state.drafts.find(draft => draft.source_job_id === id);
  state.revision = saved?.revision || 0; state.dirty = !saved;
  el("draft-source").textContent = `來源辨識 ${id}；${saved ? `已儲存修訂 ${saved.revision}` : "尚未儲存"}。`;
  const rows = saved?.rows || job.result || [];
  editRows(rows.length ? rows : [{description: "", quantity: 0}]);
  status("draft-status", saved ? `已儲存修訂 ${saved.revision}。` : "請核對後按「儲存草稿」。未儲存的編修會消失。", saved ? "good" : "");
  refreshControls();
}
function clearDraftEditor() {
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
async function upload() {
  const file = el("pdf").files?.[0]; if (!file || state.busy) return;
  if (file.size < 1 || file.size > 8 * 1024 * 1024) { status("upload-status", "PDF 須大於 0 且不超過 8 MB。", "error"); return; }
  state.busy = true; refreshControls(); status("upload-status", "正在檢查並上傳…");
  try {
    const bytes = await file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const sha = Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, "0")).join("");
    if (state.uploadFile !== file) { state.uploadFile = file; state.uploadKey = uuid(); }
    const doc = await api("management/documents", {method: "POST", body: bytes,
      headers: {"Content-Type": "application/pdf", "X-File-Size": String(bytes.byteLength),
        "X-File-SHA256": sha, "X-Request-Key": state.uploadKey}}, 30000);
    state.documents = [doc, ...state.documents.filter(item => item.id !== doc.id)];
    state.selected = doc.id; state.job = null; el("pdf").value = ""; state.uploadFile = null; state.uploadKey = null;
    renderDocuments(); clearDraft(); status("upload-status", "上傳完成；請設定金鑰後啟動辨識。", "good");
  } catch (error) { status("upload-status", `上傳未確認：${safeError(error)}。先重新載入列表確認；保留同一選檔時重試會使用相同請求識別。`, "unknown"); }
  finally { state.busy = false; refreshControls(); }
}
async function setKey() {
  const key = el("ai-key").value;
  const issue = keyInputError(key);
  if (issue) { status("key-status", issue, "error"); return; }
  try { await api("key", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key})});
    state.key = true; status("key-status", "金鑰已暫存，15 分鐘後失效。格式不代表有效；可至診斷測試頁明確執行文字連線檢查。", "good"); }
  catch (error) { status("key-status", error.code === "BAD_KEY"
    ? "輸入形式不符。請從 AI Studio 複製單一金鑰，勿貼網站密碼或網址。"
    : `設定失敗：${safeError(error)}`, "error"); }
  finally { clearKeyInput(); refreshControls(); }
}
async function clearKey() {
  try { await api("key", {method: "DELETE"}); state.key = false; status("key-status", "金鑰已清除。"); }
  catch (error) { status("key-status", `清除未確認：${safeError(error)}`, "unknown"); }
  refreshControls();
}
async function recognize() {
  if (!state.selected || !state.key || state.busy) return;
  if (!window.confirm("將這份 PDF 傳給 Google 辨識，可能使用你的 API 額度。確定送出？")) return;
  state.busy = true; refreshControls(); status("job-status", "正在建立辨識工作…");
  const documentId = state.selected;
  state.jobKey = uuid();
  try {
    const job = await api("management/jobs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_id: documentId, request_key: state.jobKey, scenario: "real"})});
    state.jobs = [job, ...state.jobs.filter(item => item.id !== job.id)]; state.job = job;
    state.jobKey = null; selectJob(job.id);
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
prepareKeyInput();
el("login-form").addEventListener("submit", login);
el("retry").addEventListener("click", load);
el("logout").addEventListener("click", async () => {
  try { await api("logout", {method: "POST"}); showLogin("已登出。"); }
  catch (error) { status("key-status", `登出未確認：${safeError(error)}。請重試。`, "unknown"); }
});
el("pdf").addEventListener("change", () => { state.uploadFile = null; state.uploadKey = null; refreshControls(); });
el("upload").addEventListener("click", upload);
el("set-key").addEventListener("click", setKey);
el("clear-key").addEventListener("click", clearKey);
el("recognize").addEventListener("click", recognize);
el("add-row").addEventListener("click", () => { if (state.rows.length >= 100) return;
  editRows([...state.rows, {description: "", quantity: 0}]); markDirty(); });
el("save-draft").addEventListener("click", saveDraft);
load();
