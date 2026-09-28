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
let basicOk = false;
let uploadUnknown = false;
let jobSubmitUnknown = false;

function setStatus(id, message, state = "") {
  const element = $(id);
  element.textContent = message;
  element.dataset.state = state;
}
function nextStep(message) { $("next-step").textContent = message; }
function reportReady() { $("report-section").classList.add("ready"); }
function fileSize(size) { return size < 1024 ? `${size} 位元組` : `${Math.ceil(size / 1024)} KB`; }
function updateUploadChoice() {
  const file = $("pdf-file").files[0];
  $("upload-button").disabled = !basicOk || !file || file.size < 1 || file.size > maxBytes || uploadBusy || uploadUnknown;
  if (!file) {
    $("selected-file").textContent = currentDocument
      ? "這份測試 PDF 已上傳。若要測另一份，請再選檔。"
      : "還沒有選檔。";
    return;
  }
  if (file.size < 1) {
    $("selected-file").textContent = "這份檔案是空的。請重新選一份測試 PDF。";
    mark("upload", "fail", {code: "BAD_SIZE"});
  } else if (file.size > maxBytes) {
    $("selected-file").textContent = "檔案超過約 8 MB，請選更小的測試 PDF。";
    mark("upload", "fail", {code: "FILE_TOO_LARGE"});
  } else {
    $("selected-file").textContent = `已選擇一份 ${fileSize(file.size)} 的檔案。請先確認沒有客戶或個人資料。`;
  }
}
function onFileChanged() {
  uploadKey = null;
  uploadUnknown = false;
  currentDocument = null;
  currentJob = null;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = true;
  $("report-section").classList.remove("ready");
  renderRows($("result-body"), []);
  for (const name of ["upload", "integrity", "ai", "format", "render"]) mark(name, "not_run");
  setStatus("ai-status", "請先完成上傳。");
  updateUploadChoice();
  const file = $("pdf-file").files[0];
  if (!file) {
    setStatus("upload-status", "尚未選檔，請選一份沒有客戶資料的測試 PDF。");
  } else if (file.size < 1 || file.size > maxBytes) {
    setStatus("upload-status", "這份檔案無法上傳。請依上方提示重新選擇。", "fail");
  } else if (basicOk) {
    setStatus("upload-status", "已選好檔案。下一步：按「確認上傳」。");
  } else {
    setStatus("upload-status", "已選好檔案。請先完成步驟 1 的連線檢查。");
  }
  nextStep(!file || file.size < 1 || file.size > maxBytes
    ? "請在步驟 2 重新選一份可用的測試 PDF。"
    : basicOk ? "現在請按步驟 2 的「確認上傳」。" : "現在請先完成步驟 1 的連線檢查。");
}
function uploadAdvice(code, unknown) {
  if (unknown || ["UPLOAD_INCOMPLETE", "REQUEST_TIMEOUT", "NETWORK_ERROR"].includes(code))
    return "還不能確認網站是否收到檔案。請先重新整理頁面查看；若沒有出現已上傳文件，請複製報告傳回提供連結的人。";
  if (["PDF_INVALID", "PDF_REQUIRED", "BAD_SIZE"].includes(code))
    return "這份檔案不是可用的 PDF。請重新選擇一份沒有個資的測試 PDF。";
  if (code === "FILE_TOO_LARGE") return "檔案太大，請選擇小於約 8 MB 的測試 PDF。";
  if (["HASH_MISMATCH", "SIZE_MISMATCH", "RECEIPT_MISMATCH"].includes(code))
    return "檔案傳送未通過完整性檢查。請重新選擇測試 PDF 再試；若仍失敗，請複製報告傳回提供連結的人。";
  if (code === "PDF_CHECK_TIMEOUT") return "網站檢查 PDF 時等太久。請換一份較小的測試 PDF；若仍失敗，請複製報告傳回提供連結的人。";
  return "上傳沒有完成。請重新選擇測試 PDF 再試；若仍失敗，請複製報告傳回提供連結的人。";
}

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
    button.textContent = `選用 ${new Date(doc.created_ms).toLocaleString("zh-TW")} 上傳的測試 PDF（${fileSize(doc.size)}）`;
    button.addEventListener("click", () => selectDocument(doc));
    list.append(button);
  }
}
function selectDocument(doc) {
  uploadUnknown = false;
  jobSubmitUnknown = false;
  currentDocument = doc;
  currentJob = jobs.find((job) => job.document_id === doc.id) || null;
  updateUploadChoice();
  $("recognize-button").disabled = !!currentJob;
  $("rerun-button").disabled = !currentJob;
  setStatus("upload-status", "測試 PDF 已完整送到提供此網站的電腦。", "pass");
  mark("upload", "pass", {ms: doc.upload_ms, http: 201});
  mark("integrity", "pass");
  if (currentJob) showJob(currentJob);
  else {
    renderRows($("result-body"), []);
    setStatus("ai-status", "已上傳。下一步：按「開始模擬測試」。");
    nextStep("現在請按步驟 3 的「開始模擬測試」。");
    updateReport();
  }
}
function showJob(job) {
  currentJob = job;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = jobBusy || ["queued", "running", "unknown"].includes(job.state);
  const duration = job.started_ms && job.finished_ms ? job.finished_ms - job.started_ms : undefined;
  mark("ai", job.steps.ai || "not_run", {ms: duration, code: job.error_code || undefined});
  mark("format", job.steps.format || "not_run");
  if (job.state === "done") {
    try {
      if (!Array.isArray(job.result)) throw new Error("format");
      renderRows($("result-body"), job.result);
      mark("render", "pass");
      setStatus("ai-status", "範例結果已顯示。這些品項與你的 PDF 內容無關。下一步：複製測試報告。", "pass");
      nextStep("已完成模擬測試。請到步驟 4 複製報告，貼給提供連結的人。");
    } catch {
      renderRows($("result-body"), []);
      mark("render", "fail", {code: "RENDER_FAILED"});
      setStatus("ai-status", "結果未能顯示。請複製測試報告傳回提供連結的人。", "fail");
      nextStep("結果顯示有問題。請到步驟 4 複製報告。");
    }
  } else {
    renderRows($("result-body"), []);
    mark("render", "not_run");
    if (["queued", "running"].includes(job.state)) {
      setStatus("ai-status", "正在進行模擬測試，請稍候，不需要再按一次。", "working");
      nextStep("正在模擬測試，請稍候。");
    } else if (job.state === "unknown") {
      setStatus("ai-status", "結果還不能確定。請稍後重新整理頁面查看；不要立刻重做。若仍不清楚，請複製報告傳回提供連結的人。", "unknown");
      nextStep("結果尚不確定。稍後重新整理；必要時到步驟 4 複製報告。");
    } else {
      setStatus("ai-status", "模擬測試沒有完成。請複製報告傳回提供連結的人。", "fail");
      nextStep("模擬測試未完成。請到步驟 4 複製報告。");
    }
  }
  if (!["queued", "running"].includes(job.state)) reportReady();
}
async function runBasic() {
  $("basic-button").disabled = true;
  setStatus("basic-status", "正在檢查連線，請稍候……", "working");
  nextStep("正在確認連線，請稍候。");
  const start = performance.now();
  mark("api", "not_run");
  mark("sample", "not_run");
  try {
    const nonce = crypto.randomUUID();
    const echo = await api("echo", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({nonce})});
    if (echo.data.nonce !== nonce) throw {code: "ECHO_MISMATCH", status: echo.status};
    mark("api", "pass", {ms: Math.round(performance.now() - start), http: echo.status});
    const sample = await api("sample");
    renderRows($("sample-body"), sample.data.rows);
    if (!$("sample-body").textContent.includes("中文 <測試>")) throw {code: "SAMPLE_RENDER_FAILED"};
    mark("sample", "pass", {http: sample.status});
    basicOk = true;
    updateUploadChoice();
    setStatus("basic-status", "連線正常，可以繼續測試。", "pass");
    if (!currentDocument) {
      const file = $("pdf-file").files[0];
      if (!file) {
        setStatus("upload-status", "連線已通過。請選一份沒有客戶資料的測試 PDF。");
        nextStep("連線正常。現在請到步驟 2 選擇測試 PDF。");
      } else if (file.size < 1 || file.size > maxBytes) {
        setStatus("upload-status", "這份檔案無法上傳。請依上方提示重新選擇。", "fail");
        nextStep("請在步驟 2 重新選一份可用的測試 PDF。");
      } else {
        setStatus("upload-status", "已選好檔案。下一步：按「確認上傳」。");
        nextStep("現在請按步驟 2 的「確認上傳」。");
      }
    }
  } catch (error) {
    const code = safeCode(error, "BASIC_FAILED");
    if (steps.api.status !== "pass") mark("api", error.unknown ? "unknown" : "fail", {code, http: error.status});
    else mark("sample", "fail", {code, http: error.status});
    basicOk = false;
    updateUploadChoice();
    if (!currentDocument) setStatus("upload-status", "連線檢查尚未通過，暫時不能上傳。請先重試步驟 1。");
    setStatus("basic-status", "連線沒有通過。請確認網路後再按一次「檢查連線」；若仍失敗，請複製測試報告傳回提供連結的人。", "fail");
    nextStep("連線檢查未通過。請重試步驟 1，或到步驟 4 複製報告。");
    reportReady();
  } finally { $("basic-button").disabled = false; }
}
async function sha256(file) {
  const buffer = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function upload() {
  if (uploadBusy || !basicOk) return;
  const file = $("pdf-file").files[0];
  if (!file) { setStatus("upload-status", "請先選擇一份沒有客戶資料的測試 PDF。", "fail"); return; }
  if (file.size > maxBytes || file.size < 1) { updateUploadChoice(); return; }
  if (!window.confirm("這份測試 PDF 會上傳並保存在提供此網站的電腦。請確認沒有客戶或個人資料；要繼續嗎？")) return;
  uploadBusy = true;
  $("upload-button").disabled = true;
  setStatus("upload-status", "正在檢查並上傳，請稍候，不需要再按一次。", "working");
  nextStep("正在上傳測試 PDF，請稍候。");
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
    $("pdf-file").value = "";
  } catch (error) {
    const code = safeCode(error, "UPLOAD_FAILED");
    uploadUnknown = !!error.unknown;
    mark("upload", error.unknown ? "unknown" : "fail", {ms: Math.round(performance.now() - start), code, http: error.status});
    setStatus("upload-status", uploadAdvice(code, error.unknown), error.unknown ? "unknown" : "fail");
    nextStep("上傳未完成。請按步驟 2 的提示處理；必要時複製報告。");
    reportReady();
  } finally { uploadBusy = false; updateUploadChoice(); }
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
      setStatus("ai-status", "暫時查不到進度。請稍後重新整理頁面；若仍不清楚，請複製報告傳回提供連結的人。", "unknown");
      nextStep("暫時查不到進度。稍後重新整理；必要時到步驟 4 複製報告。");
      reportReady();
      return;
    }
  }
  mark("query", "unknown", {code: "POLL_DEADLINE"});
  setStatus("ai-status", "等待時間已到，結果還不能確定。請稍後重新整理頁面；不要立刻重做。", "unknown");
  nextStep("結果尚不確定。稍後重新整理；必要時到步驟 4 複製報告。");
  reportReady();
}
async function recognize(rerun = false) {
  if (!currentDocument || jobBusy) return;
  if (!rerun && currentJob) { await pollJob(currentJob.id); return; }
  jobBusy = true;
  jobSubmitUnknown = false;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = true;
  setStatus("ai-status", "正在開始模擬測試，請稍候，不需要再按一次。", "working");
  nextStep("正在模擬測試，請稍候。");
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
    jobSubmitUnknown = !!error.unknown;
    mark("ai", error.unknown ? "unknown" : "fail", {code, http: error.status});
    setStatus("ai-status", error.unknown
      ? "還不能確認測試是否開始。請重新整理頁面查看；不要立刻重做。若仍不清楚，請複製報告。"
      : "模擬測試無法開始。請複製報告傳回提供連結的人。", error.unknown ? "unknown" : "fail");
    nextStep("模擬測試未完成。請到步驟 4 複製報告。");
    reportReady();
  } finally {
    jobBusy = false;
    $("recognize-button").disabled = !currentDocument || !!currentJob;
    $("rerun-button").disabled = !currentJob || jobSubmitUnknown || ["queued", "running", "unknown"].includes(currentJob.state);
  }
}
async function copyReport() {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("unavailable");
    mark("clipboard", "pass");
    await navigator.clipboard.writeText($("report").value);
    setStatus("copy-status", "報告已複製。請貼給提供連結的人。", "pass");
  } catch {
    mark("clipboard", "fail", {code: "CLIPBOARD_UNAVAILABLE"});
    $("report-details").open = true;
    $("report").focus();
    $("report").select();
    setStatus("copy-status", "無法自動複製。請手動複製：下方報告已選取，按 Ctrl+C（Mac 用 Cmd+C），再貼給提供連結的人。", "fail");
  }
}
async function initialize() {
  updateReport();
  const styleLoaded = getComputedStyle(document.querySelector("header")).backgroundColor !== "rgba(0, 0, 0, 0)";
  mark("style", styleLoaded ? "pass" : "fail", styleLoaded ? {} : {code: "STYLE_MISSING"});
  $("basic-button").addEventListener("click", runBasic);
  $("upload-button").addEventListener("click", upload);
  $("pdf-file").addEventListener("change", onFileChanged);
  $("recognize-button").addEventListener("click", () => recognize(false));
  $("rerun-button").addEventListener("click", () => { jobKey = null; recognize(true); });
  $("copy-button").addEventListener("click", copyReport);
  updateUploadChoice();
  try {
    const response = await api("bootstrap");
    version = response.data.version;
    maxBytes = response.data.max_pdf_bytes;
    documents = response.data.documents;
    jobs = response.data.jobs;
    mark("query", "pass", {http: response.status});
    renderDocuments();
    if (documents.length) {
      selectDocument(documents[0]);
      setStatus("basic-status", "已找回先前的上傳紀錄；若要測新檔案，請先按「檢查連線」。");
    }
  } catch (error) {
    mark("api", error.unknown ? "unknown" : "fail", {code: safeCode(error, "BOOTSTRAP_FAILED")});
    setStatus("basic-status", "現在無法連上網站服務。請稍後重新整理；若仍失敗，請複製報告傳回提供連結的人。", "fail");
    nextStep("目前無法連線。稍後重新整理，或到步驟 4 複製報告。");
    reportReady();
  }
}
initialize();
