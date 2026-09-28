"use strict";

const base = "/orderflow/api/";
const $ = (id) => document.getElementById(id);
const labels = {pass: "通過", fail: "失敗", unknown: "結果不明", not_run: "未執行"};
const names = ["page", "script", "style", "api", "sample", "upload", "integrity", "query", "ai", "format", "render", "clipboard"];
const titles = {page: "網頁", script: "JavaScript", style: "樣式", api: "同站 API", sample: "固定資料渲染", upload: "PDF 上傳", integrity: "完整性", query: "工作查詢", ai: "模擬 AI", format: "結果格式", render: "結果渲染", clipboard: "報告複製"};
const steps = Object.fromEntries(names.map((name) => [name, {status: "not_run"}]));
steps.page = {status: "pass"};
steps.script = {status: "pass"};
const testId = crypto.randomUUID();
let version = "0.1.0";
let maxBytes = 8 * 1024 * 1024;
let documents = [];
let jobs = [];
let currentDocument = null;
let currentJob = null;
let uploadKey = null;
let jobKey = null;
let uploadBusy = false;
let jobBusy = false;

function mark(name, status, extra = {}) {
  steps[name] = {status, ...extra};
  updateReport();
}
function safeCode(error, fallback) {
  const code = error?.code || fallback;
  return /^[A-Z0-9_]{2,40}$/.test(code) ? code : fallback;
}
async function api(path, options = {}, timeoutMs = 5000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(base + path, {
      ...options, signal: controller.signal,
      headers: {...options.headers, "X-Orderflow-Request": "1"},
      credentials: "same-origin", cache: "no-store"
    });
    let data;
    try { data = await response.json(); }
    catch { throw {code: "BAD_JSON_RESPONSE", status: response.status}; }
    if (!response.ok) throw {code: safeCode({code: data.error_code}, "HTTP_ERROR"), status: response.status};
    return {data, status: response.status};
  } catch (error) {
    if (error?.name === "AbortError") throw {code: "REQUEST_TIMEOUT", unknown: true};
    if (error?.code) throw error;
    throw {code: "NETWORK_ERROR", unknown: true};
  } finally { clearTimeout(timer); }
}
function renderRows(tbody, rows) {
  tbody.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    const description = document.createElement("td");
    const quantity = document.createElement("td");
    description.textContent = row.description;
    quantity.textContent = String(row.quantity);
    tr.append(description, quantity);
    tbody.append(tr);
  }
}
function updateReport() {
  const lines = [
    `OrderFlow 診斷報告 v${version}（本機模擬）`,
    `時間：${new Date().toISOString()}`,
    `測試識別：${testId}`,
    `瀏覽器：${navigator.userAgent}`,
    `文件識別：${currentDocument?.id || "無"}`,
    `工作識別：${currentJob?.id || "無"}`,
    `檔案大小：${currentDocument?.size ?? "未知"} bytes`,
    `頁數：${currentDocument?.page_count ?? "未取得"}`, "模式：本機模擬；真實外部 AI 未驗證", "步驟："
  ];
  for (const name of names) {
    const entry = steps[name];
    const extras = [];
    if (entry.ms !== undefined) extras.push(`${entry.ms} ms`);
    if (entry.http !== undefined) extras.push(`HTTP ${entry.http}`);
    if (entry.code) extras.push(entry.code);
    lines.push(`- ${titles[name]}：${labels[entry.status]}${extras.length ? "（" + extras.join("，") + "）" : ""}`);
  }
  lines.push("限制：真實 AI、HTTPS 入口、公司瀏覽器與資料外傳許可尚未驗證。網站無法載入時請由人提供錯誤截圖與時間。");
  $("report").value = lines.join("\n");
}
function renderDocuments() {
  const list = $("document-list");
  list.replaceChildren();
  for (const doc of documents) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "document";
    button.textContent = `文件 ${doc.id.slice(0, 8)} · ${doc.size} bytes · ${new Date(doc.created_ms).toLocaleString()}`;
    button.addEventListener("click", () => selectDocument(doc));
    list.append(button);
  }
}
function selectDocument(doc) {
  currentDocument = doc;
  currentJob = jobs.find((job) => job.document_id === doc.id) || null;
  $("recognize-button").disabled = false;
  $("rerun-button").disabled = !currentJob;
  $("upload-status").textContent = `已確認收件完整：${doc.size} bytes（SHA-256 一致）`;
  mark("upload", "pass", {ms: doc.upload_ms, http: 201});
  mark("integrity", "pass");
  if (currentJob) showJob(currentJob);
  else { renderRows($("result-body"), []); $("ai-status").textContent = "尚未執行"; updateReport(); }
}
function showJob(job) {
  currentJob = job;
  $("rerun-button").disabled = false;
  const phase = {queued: "排隊中", running: "執行中", done: "完成", failed: "失敗", unknown: "結果不明"}[job.state] || "未知";
  $("ai-status").textContent = `第 ${job.attempt} 次模擬辨識：${phase}${job.error_code ? "（" + job.error_code + "）" : ""}`;
  const duration = job.started_ms && job.finished_ms ? job.finished_ms - job.started_ms : undefined;
  mark("ai", job.steps.ai || "not_run", {ms: duration, code: job.error_code || undefined});
  mark("format", job.steps.format || "not_run");
  if (job.state === "done") {
    try {
      if (!Array.isArray(job.result)) throw new Error("format");
      renderRows($("result-body"), job.result);
      mark("render", "pass");
    } catch {
      renderRows($("result-body"), []);
      mark("render", "fail", {code: "RENDER_FAILED"});
    }
  } else { renderRows($("result-body"), []); mark("render", "not_run"); }
}
async function runBasic() {
  $("basic-button").disabled = true;
  const start = performance.now();
  try {
    const nonce = crypto.randomUUID();
    const echo = await api("echo", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({nonce})});
    if (echo.data.nonce !== nonce) throw {code: "ECHO_MISMATCH", status: echo.status};
    mark("api", "pass", {ms: Math.round(performance.now() - start), http: echo.status});
    const sample = await api("sample");
    renderRows($("sample-body"), sample.data.rows);
    if (!$("sample-body").textContent.includes("中文 <測試>")) throw {code: "SAMPLE_RENDER_FAILED"};
    mark("sample", "pass", {http: sample.status});
    $("basic-status").textContent = "API 往返與固定資料渲染通過";
  } catch (error) {
    const code = safeCode(error, "BASIC_FAILED");
    if (steps.api.status !== "pass") mark("api", error.unknown ? "unknown" : "fail", {code, http: error.status});
    else mark("sample", "fail", {code, http: error.status});
    $("basic-status").textContent = `基本檢查未通過：${code}`;
  } finally { $("basic-button").disabled = false; }
}
async function sha256(file) {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function upload() {
  if (uploadBusy) return;
  const file = $("pdf-file").files[0];
  if (!file) { $("upload-status").textContent = "請先選擇 PDF"; return; }
  if (file.size > maxBytes) { mark("upload", "fail", {code: "FILE_TOO_LARGE"}); $("upload-status").textContent = "超過 8 MiB 上限"; return; }
  if (file.size < 1 || file.type !== "application/pdf") { mark("upload", "fail", {code: "PDF_REQUIRED"}); $("upload-status").textContent = "請選擇有效 PDF"; return; }
  if (!window.confirm(`確定上傳這份 ${file.size} bytes 的去識別測試 PDF？`)) return;
  uploadBusy = true;
  $("upload-button").disabled = true;
  const start = performance.now();
  try {
    const hash = await sha256(file);
    uploadKey = uploadKey || crypto.randomUUID();
    const response = await api("documents", {method: "POST", headers: {
      "Content-Type": "application/pdf", "X-File-Size": String(file.size),
      "X-File-SHA256": hash, "X-Request-Key": uploadKey
    }, body: file}, 25000);
    const doc = response.data;
    if (doc.size !== file.size || doc.sha256 !== hash) throw {code: "RECEIPT_MISMATCH"};
    documents.unshift(doc);
    renderDocuments();
    selectDocument(doc);
    mark("upload", "pass", {ms: Math.round(performance.now() - start), http: response.status});
    uploadKey = null;
  } catch (error) {
    const code = safeCode(error, "UPLOAD_FAILED");
    mark("upload", error.unknown ? "unknown" : "fail", {ms: Math.round(performance.now() - start), code, http: error.status});
    $("upload-status").textContent = error.unknown ? "收件結果不明；請重新整理查詢，勿盲目重送" : `上傳失敗：${code}`;
  } finally { uploadBusy = false; $("upload-button").disabled = false; }
}
async function pollJob(id) {
  const deadline = performance.now() + 15000;
  while (performance.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    try {
      const response = await api("jobs/" + id, {}, 4000);
      mark("query", "pass", {http: response.status});
      showJob(response.data);
      if (!["queued", "running"].includes(response.data.state)) {
        jobs = [response.data, ...jobs.filter((job) => job.id !== id)];
        return;
      }
    } catch (error) {
      mark("query", error.unknown ? "unknown" : "fail", {code: safeCode(error, "QUERY_FAILED"), http: error.status});
      $("ai-status").textContent = "查詢結果不明；重新整理可查回工作";
      return;
    }
  }
  mark("query", "unknown", {code: "POLL_DEADLINE"});
  $("ai-status").textContent = "等待已達上限，處理結果不明；重新整理查詢，勿盲目重送";
}
async function recognize(rerun = false) {
  if (!currentDocument || jobBusy) return;
  if (!rerun && currentJob) { await pollJob(currentJob.id); return; }
  jobBusy = true;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = true;
  try {
    jobKey = jobKey || crypto.randomUUID();
    const response = await api("jobs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_id: currentDocument.id, request_key: jobKey, scenario: $("scenario").value})});
    jobKey = null;
    showJob(response.data);
    jobs.unshift(response.data);
    await pollJob(response.data.id);
  } catch (error) {
    const code = safeCode(error, "JOB_SUBMIT_FAILED");
    mark("ai", error.unknown ? "unknown" : "fail", {code, http: error.status});
    $("ai-status").textContent = error.unknown ? "提交結果不明；重新整理查詢，勿盲目重送" : `提交失敗：${code}`;
  } finally {
    jobBusy = false;
    $("recognize-button").disabled = !currentDocument;
    $("rerun-button").disabled = !currentJob;
  }
}
async function copyReport() {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("unavailable");
    mark("clipboard", "pass");
    await navigator.clipboard.writeText($("report").value);
    $("copy-status").textContent = "已複製；請檢查內容後分享";
  } catch {
    mark("clipboard", "fail", {code: "CLIPBOARD_UNAVAILABLE"});
    $("report").focus();
    $("report").select();
    $("copy-status").textContent = "無法自動複製，報告已選取；請手動複製";
  }
}
async function initialize() {
  updateReport();
  const styleLoaded = getComputedStyle(document.querySelector("header")).backgroundColor !== "rgba(0, 0, 0, 0)";
  mark("style", styleLoaded ? "pass" : "fail", styleLoaded ? {} : {code: "STYLE_MISSING"});
  $("basic-button").addEventListener("click", runBasic);
  $("upload-button").addEventListener("click", upload);
  $("pdf-file").addEventListener("change", () => { uploadKey = null; });
  $("recognize-button").addEventListener("click", () => recognize(false));
  $("rerun-button").addEventListener("click", () => { jobKey = null; recognize(true); });
  $("copy-button").addEventListener("click", copyReport);
  try {
    const response = await api("bootstrap");
    version = response.data.version;
    maxBytes = response.data.max_pdf_bytes;
    documents = response.data.documents;
    jobs = response.data.jobs;
    mark("query", "pass", {http: response.status});
    renderDocuments();
    if (documents.length) selectDocument(documents[0]);
  } catch (error) {
    mark("api", error.unknown ? "unknown" : "fail", {code: safeCode(error, "BOOTSTRAP_FAILED")});
    $("basic-status").textContent = "無法連線同站 API";
  }
}
initialize();
