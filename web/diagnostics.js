"use strict";
// Page-local evidence only. Nothing here is an authorization or delivery receipt.
window.OrderflowDiagnostics = (() => {
  const buildId = "diag-20261002-01";
  const routes = new Set(["bootstrap", "management_bootstrap", "documents",
    "management_documents", "jobs", "management_jobs", "job_get",
    "management_job_get", "local_sources", "record_sets", "drafts",
    "key_check", "echo", "sample", "diagnostics", "other_api"]);
  const phases = new Set(["send", "http_received", "json_parsed", "marker_checked",
    "job_rendered", "request_unknown", "json_failed", "render_failed"]);
  const codes = new Set(["NONE", "HTTP_ERROR", "BAD_JSON_RESPONSE", "NETWORK_ERROR",
    "REQUEST_TIMEOUT", "REQUEST_ID_MISMATCH", "ECHO_MISMATCH", "SAMPLE_RENDER_FAILED",
    "RECEIPT_MISMATCH", "JOB_SUBMIT_FAILED", "QUERY_FAILED", "POLL_DEADLINE",
    "RENDER_FAILED", "AI_UNAVAILABLE", "AI_TIMEOUT_UNKNOWN", "AI_NOT_CONFIGURED",
    "AI_RATE_LIMITED", "AI_HTTP_ERROR", "AI_BAD_RESPONSE", "AI_AUTH_FAILED",
    "AI_MODEL_UNAVAILABLE", "AI_BAD_REQUEST", "AI_HTTP_UNKNOWN", "AI_RESULT_UNKNOWN",
    "AI_FAILURE", "RESULT_FORMAT_INVALID", "INTERNAL_UNKNOWN", "KEY_REQUIRED", "JOB_LIMIT"]);
  const uuid = value => typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value);
  function route(path) {
    const fixed = {"management/bootstrap": "management_bootstrap",
      "management/documents": "management_documents", "management/jobs": "management_jobs",
      "management/local-sources": "local_sources", "key/check": "key_check"};
    if (fixed[path]) return fixed[path];
    if (path.startsWith("management/jobs/")) return "management_job_get";
    if (path.startsWith("management/record-sets/")) return "record_sets";
    if (path.startsWith("management/drafts/")) return "drafts";
    if (path.startsWith("jobs/")) return "job_get";
    return routes.has(path) ? path : "other_api";
  }
  function create(base) {
    let traceId = crypto.randomUUID();
    let operationStartedUtc = new Date().toISOString();
    const events = [];
    const important = [];
    let sent = 0;
    let lastFlush = 0;
    let autoEnabled = true;
    function newOperation() {
      traceId = crypto.randomUUID(); operationStartedUtc = new Date().toISOString(); return traceId;
    }
    function record(phase, path, details = {}) {
      if (!phases.has(phase)) return;
      const routeName = route(path);
      const poll = routeName === "job_get" || routeName === "management_job_get";
      if (poll && (phase === "send" || phase === "marker_checked" && details.marker === "APP" ||
          phase === "http_received" && details.httpStatus === 200 && details.responseType === "JSON" ||
          phase === "json_parsed" && details.httpStatus === 200)) return;
      const event = {phase, route: routeName, trace_id: traceId};
      if (uuid(details.requestId)) event.request_id = details.requestId;
      if (Number.isInteger(details.httpStatus) && details.httpStatus >= 100 &&
          details.httpStatus <= 599) event.http_status = details.httpStatus;
      if (Number.isInteger(details.durationMs))
        event.duration_ms = Math.max(0, Math.min(120000, details.durationMs));
      if (["JSON", "HTML", "TEXT", "OTHER", "MISSING"].includes(details.responseType))
        event.response_type = details.responseType;
      if (["APP", "MISSING"].includes(details.marker)) event.marker = details.marker;
      if (codes.has(details.code)) event.code = details.code;
      events.push(event);
      if (routeName === "jobs" || routeName === "management_jobs" ||
          ["request_unknown", "json_failed", "render_failed", "job_rendered"].includes(phase) ||
          phase === "http_received" && event.http_status >= 400 ||
          phase === "marker_checked" && event.marker === "MISSING") {
        important.push(event);
        if (important.length > 16) important.shift();
      }
      if (events.length > 24) { events.shift(); sent = Math.max(0, sent - 1); }
    }
    async function flush(appReached) {
      if (!appReached || !autoEnabled || sent >= events.length ||
          Date.now() - lastFlush < 5000) return;
      lastFlush = Date.now();
      const batch = events.slice(sent, sent + 12);
      sent += batch.length;
      try {
        const response = await fetch(base + "diagnostics", {
          method: "POST", credentials: "same-origin", cache: "no-store",
          headers: {"Content-Type": "application/json", "X-Orderflow-Request": "1",
            "X-Orderflow-Request-Id": crypto.randomUUID(),
            "X-Orderflow-Trace-Id": traceId},
          body: JSON.stringify({events: batch})
        });
        if (!response.ok) autoEnabled = false;
      } catch { autoEnabled = false; }
    }
    function summary() {
      const line = event => Object.entries(event).map(([key, value]) => key + "=" + value).join(" ");
      return ["診斷腳本版本：" + buildId, "操作開始（瀏覽器 UTC）：" + operationStartedUtc,
        "複製時間（瀏覽器 UTC）：" + new Date().toISOString(), "追蹤識別：" + traceId,
        "重要事件（最多 16 筆）：", ...important.map(line),
        "最近其他事件（最多 24 筆；一般輪詢已省略；不代表伺服器已收到）：",
        ...events.filter(event => !important.includes(event)).map(line)].join("\n");
    }
    return {record, flush, summary, newOperation, get traceId() {return traceId;}};
  }
  return {create, route};
})();
