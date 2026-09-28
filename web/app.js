"use strict";

const base = "/orderflow/api/";
const $ = (id) => document.getElementById(id);
const labels = {pass: "通過", fail: "失敗", unknown: "結果不明", not_run: "未執行"};
const names = ["page", "script", "style", "api", "sample", "upload", "integrity", "query", "text_check", "ai", "format", "render", "clipboard"];
const titles = {page: "網頁", script: "JavaScript", style: "樣式", api: "同站 API", sample: "固定資料渲染", upload: "PDF 上傳", integrity: "完整性", query: "工作查詢", text_check: "Google 文字連線", ai: "AI 辨識", format: "結果格式", render: "結果渲染", clipboard: "報告複製"};
const steps = Object.fromEntries(names.map((name) => [name, {status: "not_run"}]));
steps.page = {status: "pass"};
steps.script = {status: "pass"};
const testId = crypto.randomUUID();
let version = "0.3.0";
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
let aiKeyConfigured = false;
let keyBusy = false;

function showLogin(message = "請輸入網站登入密碼，才能查看測試資料。") {
  $("workspace").hidden = true;
  $("login-section").hidden = false;
  $("login-password").value = "";
  $("ai-key").value = "";
  $("report").value = "";
  documents = [];
  jobs = [];
  currentDocument = null;
  currentJob = null;
  aiKeyConfigured = false;
  setStatus("login-status", message);
}
async function login(event) {
  event.preventDefault();
  const password = $("login-password").value;
  $("login-password").value = "";
  $("login-button").disabled = true;
  setStatus("login-status", "正在登入…", "working");
  try {
    await api("login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({password})});
    location.reload();
  } catch (error) {
    const message = error.code === "LOGIN_RATE_LIMITED" ? "嘗試次數過多，請一分鐘後再試。"
      : error.code === "INVALID_CREDENTIALS" ? "網站密碼不正確，請重新輸入。"
      : "暫時無法登入，請稍後重試。";
    setStatus("login-status", message, "fail");
    $("login-button").disabled = false;
  }
}
async function logout() {
  $("logout-button").disabled = true;
  try {
    await api("logout", {method: "POST"});
    location.reload();
  } catch {
    $("logout-button").disabled = false;
    nextStep("登出尚未完成，請再試一次；暫時不要把裝置交給其他人。");
  }
}

function setStatus(id, message, state = "") {
  const element = $(id);
  element.textContent = message;
  element.dataset.state = state;
}
function nextStep(message) { $("next-step").textContent = message; }
function reportReady() { $("report-section").classList.add("ready"); }
function updateRealControls() {
  $("clear-key").disabled = !aiKeyConfigured || keyBusy;
  $("check-key").disabled = !aiKeyConfigured || keyBusy;
  $("real-button").disabled = !aiKeyConfigured || !currentDocument || jobBusy || jobSubmitUnknown
    || (!!currentJob && ["queued", "running", "unknown"].includes(currentJob.state));
}
function keyAdvice(code) {
  if (code === "AI_AUTH_FAILED") return "金鑰無效或沒有權限。請在 AI Studio 核對後重新設定。";
  if (code === "AI_RATE_LIMITED") return "Google 的額度或速率限制已達上限。請稍後再試。";
  if (code === "AI_MODEL_UNAVAILABLE") return "目前金鑰無法使用固定的 Gemini 3.1 Flash-Lite 模型，請回報提供連結的人。";
  if (code === "AI_BAD_REQUEST") return "Google 無法處理這次請求。請複製報告傳回提供連結的人。";
  if (code === "KEY_REQUIRED") return "金鑰已失效或服務已重新啟動，請重新設定。";
  if (code === "KEY_CAPACITY") return "目前暫存金鑰的人數已達上限，請稍後再試。";
  return "目前無法確認 Google 連線。請稍後再試；若仍失敗，請複製報告傳回提供連結的人。";
}
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
  updateRealControls();
  $("report-section").classList.remove("ready");
  renderRows($("result-body"), []);
  $("result-mode").textContent = "尚未產生結果。";
  for (const name of ["upload", "integrity", "ai", "format", "render"]) mark(name, "not_run");
  setStatus("ai-status", "請先完成上傳。");
  setStatus("real-status", "請先上傳測試 PDF。");
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
  if (code === "STORAGE_LIMIT") return "測試空間或本次上傳次數已達上限。請複製報告傳回提供連結的人，不要再上傳。";
  if (code === "UPLOAD_BUSY") return "目前有人正在上傳。請稍後再試。";
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
    if (!response.ok) {
      if (response.status === 401 && path !== "login" && path !== "logout") {
        showLogin(data.error_code === "SESSION_EXPIRED" ? "登入已過期，請重新輸入網站密碼。" : undefined);
      }
      throw {code: safeCode({code: data.error_code}, "HTTP_ERROR"), status: response.status};
    }
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
    `OrderFlow 診斷報告 v${version}`,
    `時間：${new Date().toISOString()}`,
    `測試識別：${testId}`,
    `瀏覽器：${navigator.userAgent}`,
    `文件識別：${currentDocument?.id || "無"}`,
    `工作識別：${currentJob?.id || "無"}`,
    `檔案大小：${currentDocument?.size ?? "未知"} bytes`,
    `頁數：${currentDocument?.page_count ?? "未取得"}`,
    `辨識模式：${currentJob?.mode === "real" ? "Google Gemini PDF 請求；內容仍需人工核對" : "固定資料模擬"}`,
    "步驟："
  ];
  for (const name of names) {
    const entry = steps[name];
    const extras = [];
    if (entry.ms !== undefined) extras.push(`${entry.ms} ms`);
    if (entry.http !== undefined) extras.push(`HTTP ${entry.http}`);
    if (entry.code) extras.push(entry.code);
    lines.push(`- ${titles[name]}：${labels[entry.status]}${extras.length ? "（" + extras.join("，") + "）" : ""}`);
  }
  lines.push("限制：Google 模型可用性依金鑰；辨識內容需人工核對。公司瀏覽器與資料外傳許可尚未驗證。網站無法載入時請由人提供錯誤截圖與時間。");
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
  updateRealControls();
  $("recognize-button").disabled = !!currentJob;
  $("rerun-button").disabled = !currentJob;
  setStatus("upload-status", "測試 PDF 已完整送到提供此網站的電腦。", "pass");
  setStatus("real-status", aiKeyConfigured ? "可以按下方按鈕開始真正辨識。" : "請先設定金鑰才能開始真正辨識。");
  mark("upload", "pass", {ms: doc.upload_ms, http: 201});
  mark("integrity", "pass");
  if (currentJob) showJob(currentJob);
  else {
    renderRows($("result-body"), []);
    $("result-mode").textContent = "尚未產生結果。";
    setStatus("ai-status", "已上傳。可先試模擬測試，或設定金鑰後試真正辨識。");
    nextStep("現在可按步驟 3 的模擬測試，或設定金鑰後試真正辨識。");
    updateReport();
  }
}
function showJob(job) {
  currentJob = job;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = jobBusy || ["queued", "running", "unknown"].includes(job.state);
  updateRealControls();
  const kind = job.mode === "real" ? "Google PDF 辨識" : "模擬測試";
  $("result-mode").textContent = job.mode === "real"
    ? "下表是 Google 回傳的 PDF 辨識結果，請人工核對。"
    : "下表是固定模擬範例，與 PDF 內容無關。";
  const duration = job.started_ms && job.finished_ms ? job.finished_ms - job.started_ms : undefined;
  mark("ai", job.steps.ai || "not_run", {ms: duration, code: job.error_code || undefined});
  mark("format", job.steps.format || "not_run");
  if (job.state === "done") {
    try {
      if (!Array.isArray(job.result)) throw new Error("format");
      renderRows($("result-body"), job.result);
      mark("render", "pass");
      setStatus("ai-status", job.mode === "real"
        ? "Google 回傳的 PDF 品項已顯示；請人工核對內容。下一步：複製測試報告。"
        : "範例結果已顯示。這些品項與你的 PDF 內容無關。下一步：複製測試報告。", "pass");
      nextStep(`${kind}已完成。請核對品項，再到步驟 4 複製報告。`);
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
      setStatus("ai-status", `正在進行${kind}，請稍候，不需要再按一次。`, "working");
      nextStep(`正在進行${kind}，請稍候。`);
    } else if (job.state === "unknown") {
      setStatus("ai-status", "結果還不能確定。請稍後重新整理頁面查看；不要立刻重做。若仍不清楚，請複製報告傳回提供連結的人。", "unknown");
      nextStep("結果尚不確定。稍後重新整理；必要時到步驟 4 複製報告。");
    } else {
      setStatus("ai-status", job.mode === "real" && job.error_code
        ? keyAdvice(job.error_code) : `${kind}沒有完成。請複製報告傳回提供連結的人。`, "fail");
      nextStep(`${kind}未完成。請到步驟 4 複製報告。`);
    }
  }
  if (job.mode === "real") setStatus("real-status", $("ai-status").textContent, $("ai-status").dataset.state);
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
  const deadline = performance.now() + (currentJob?.mode === "real" ? 35000 : 15000);
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
async function saveKey() {
  const key = $("ai-key").value;
  if (!key) { setStatus("key-status", "請先貼上你的 AI Studio API key。", "fail"); return; }
  keyBusy = true;
  $("save-key").disabled = true;
  setStatus("key-status", "正在設定金鑰……", "working");
  try {
    await api("key", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key})});
    aiKeyConfigured = true;
    mark("text_check", "not_run");
    setStatus("key-status", "金鑰已設定，15 分鐘後失效；頁面不會再顯示金鑰。", "pass");
    setStatus("real-status", currentDocument ? "可以按下方按鈕開始真正辨識。" : "請先上傳測試 PDF。");
  } catch (error) {
    setStatus("key-status", error.code === "KEY_CAPACITY" ? keyAdvice("KEY_CAPACITY") : "金鑰未設定。請確認格式與連線後再試。", "fail");
  } finally {
    $("ai-key").value = "";
    keyBusy = false;
    $("save-key").disabled = false;
    updateRealControls();
  }
}
async function clearKey() {
  keyBusy = true;
  updateRealControls();
  try {
    await api("key", {method: "DELETE"});
    aiKeyConfigured = false;
    mark("text_check", "not_run");
    setStatus("key-status", "金鑰已清除。已開始的請求可能仍在處理；後續辨識需要重新設定。", "pass");
    setStatus("real-status", "請重新設定金鑰才能開始新的 Google 辨識。");
    setStatus("check-status", "金鑰已清除；若要再做文字連線檢查，請重新設定。");
  } catch { setStatus("key-status", "暫時無法確認金鑰已清除，請重新整理後再試。", "unknown"); }
  finally { keyBusy = false; updateRealControls(); }
}
async function checkKey() {
  if (!aiKeyConfigured) return;
  keyBusy = true;
  updateRealControls();
  setStatus("check-status", "正在確認 Google 文字連線，請稍候……", "working");
  try {
    await api("key/check", {method: "POST"}, 15000);
    mark("text_check", "pass");
    setStatus("check-status", "文字連線成功；這還不能證明 PDF 辨識成功。", "pass");
  } catch (error) {
    if (error.code === "KEY_REQUIRED") aiKeyConfigured = false;
    const uncertain = error.unknown || ["AI_HTTP_UNKNOWN", "AI_TIMEOUT_UNKNOWN"].includes(error.code);
    mark("text_check", uncertain ? "unknown" : "fail", {code: safeCode(error, "AI_HTTP_UNKNOWN")});
    setStatus("check-status", keyAdvice(safeCode(error, "AI_HTTP_UNKNOWN")), uncertain ? "unknown" : "fail");
  } finally { keyBusy = false; updateRealControls(); }
}
function runReal() {
  if (!currentDocument || !aiKeyConfigured || jobBusy || jobSubmitUnknown) return;
  if (!window.confirm("這次會將已上傳的測試 PDF 傳給 Google Gemini，可能使用你的 API 額度。請確認檔案已獲准外傳且沒有客戶或個人資料；要開始嗎？")) return;
  jobKey = null;
  recognize(true, true);
}

async function recognize(rerun = false, real = false) {
  if (!currentDocument || jobBusy || real && !aiKeyConfigured) return;
  if (!rerun && currentJob) { await pollJob(currentJob.id); return; }
  jobBusy = true;
  jobSubmitUnknown = false;
  $("recognize-button").disabled = true;
  $("rerun-button").disabled = true;
  updateRealControls();
  setStatus("ai-status", real ? "正在開始 Google PDF 辨識，請稍候，不需要再按一次。" : "正在開始模擬測試，請稍候，不需要再按一次。", "working");
  nextStep(real ? "Google 正在辨識 PDF，請稍候。" : "正在模擬測試，請稍候。");
  try {
    jobKey = jobKey || crypto.randomUUID();
    const response = await api("jobs", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({document_id: currentDocument.id, request_key: jobKey, scenario: real ? "real" : $("scenario").value})});
    jobKey = null;
    showJob(response.data);
    jobs.unshift(response.data);
    await pollJob(response.data.id);
  } catch (error) {
    const code = safeCode(error, "JOB_SUBMIT_FAILED");
    jobSubmitUnknown = !!error.unknown;
    if (error.code === "KEY_REQUIRED") {
      aiKeyConfigured = false;
      setStatus("key-status", keyAdvice("KEY_REQUIRED"), "fail");
    }
    mark("ai", error.unknown ? "unknown" : "fail", {code, http: error.status});
    setStatus("ai-status", error.unknown
      ? "還不能確認測試是否開始。請重新整理頁面查看；不要立刻重做。若仍不清楚，請複製報告。"
      : error.code === "JOB_LIMIT" ? "這份 PDF 的測試次數已達上限，請複製報告傳回提供連結的人。"
      : real ? "Google 辨識無法開始。請檢查金鑰狀態或複製報告。"
      : "模擬測試無法開始。請複製報告傳回提供連結的人。", error.unknown ? "unknown" : "fail");
    nextStep(`${real ? "Google PDF 辨識" : "模擬測試"}未完成。請到步驟 4 複製報告。`);
    reportReady();
  } finally {
    jobBusy = false;
    $("recognize-button").disabled = !currentDocument || !!currentJob;
    $("rerun-button").disabled = !currentJob || jobSubmitUnknown || ["queued", "running", "unknown"].includes(currentJob.state);
    updateRealControls();
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
  $("login-form").addEventListener("submit", login);
  $("logout-button").addEventListener("click", logout);
  updateReport();
  const styleLoaded = getComputedStyle(document.querySelector("header")).backgroundColor !== "rgba(0, 0, 0, 0)";
  mark("style", styleLoaded ? "pass" : "fail", styleLoaded ? {} : {code: "STYLE_MISSING"});
  $("basic-button").addEventListener("click", runBasic);
  $("upload-button").addEventListener("click", upload);
  $("pdf-file").addEventListener("change", onFileChanged);
  $("recognize-button").addEventListener("click", () => recognize(false));
  $("rerun-button").addEventListener("click", () => { jobKey = null; recognize(true); });
  $("copy-button").addEventListener("click", copyReport);
  $("save-key").addEventListener("click", saveKey);
  $("clear-key").addEventListener("click", clearKey);
  $("check-key").addEventListener("click", checkKey);
  $("real-button").addEventListener("click", runReal);
  updateUploadChoice();
  updateRealControls();
  try {
    const response = await api("bootstrap");
    $("login-section").hidden = true;
    $("workspace").hidden = false;
    version = response.data.version;
    maxBytes = response.data.max_pdf_bytes;
    documents = response.data.documents;
    jobs = response.data.jobs;
    aiKeyConfigured = !!response.data.ai_key_configured;
    setStatus("key-status", aiKeyConfigured
      ? "金鑰仍暫存在此服務的記憶體；到期或重新啟動後需重新輸入。"
      : "尚未設定金鑰；仍可使用上方模擬測試。", aiKeyConfigured ? "pass" : "");
    updateRealControls();
    mark("query", "pass", {http: response.status});
    renderDocuments();
    if (documents.length) {
      selectDocument(documents[0]);
      setStatus("basic-status", "已找回先前的上傳紀錄；若要測新檔案，請先按「檢查連線」。");
    }
  } catch (error) {
    if (error.status === 401) return;
    mark("api", error.unknown ? "unknown" : "fail", {code: safeCode(error, "BOOTSTRAP_FAILED")});
    setStatus("basic-status", "現在無法連上網站服務。請稍後重新整理；若仍失敗，請複製報告傳回提供連結的人。", "fail");
    nextStep("目前無法連線。稍後重新整理，或到步驟 4 複製報告。");
    reportReady();
  }
}
initialize();
