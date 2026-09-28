# 2026-09-28 Google 文字連線診斷

使用者於正式 v0.3.0 網站執行文字連線檢查，安全診斷報告顯示 `Google 文字連線：失敗（BAD_JSON_RESPONSE）`。這代表瀏覽器無法將**本站** `/orderflow/api/key/check` 的回應解析為 JSON；報告沒有該請求 HTTP status、耗時或 Content-Type，因此無法從這份報告判定是哪一層回了非 JSON。另一次固定資料模擬得到 `AI_UNAVAILABLE`，與 Google 請求無關。沒有重播使用者金鑰、讀取程序記憶體或檢視 PDF 內容。

唯讀檢查：ASUS app、HP Caddy、SSH tunnel 均 active，NRestarts=0；該時段三服務沒有可用的請求日誌。ASUS 能解析 `generativelanguage.googleapis.com`，無憑證 GET 完成 TLS 驗證並收到 HTTP 404；這只證明 DNS/TLS/出口可達。匿名 POST 同一路徑經公開 HTTPS 與 HP Caddy 都回本站 JSON 401，不證明帶 session/key 的請求成功。Google 官方 [模型頁](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite) 與 [GenerateContent API](https://ai.google.dev/api/generate-content) 顯示固定 model code 與 REST request 形狀和目前 adapter 一致；尚無真實 Google 成功證據。

已證實的程式缺陷：Google HTTP 讀取若拋 `http.client.HTTPException`（合成 `IncompleteRead`），原 adapter 未捕獲，本站 request handler 直接斷線；本機測試收到 `RemoteDisconnected` 而非 JSON。這條路徑**可能**造成代理的非 JSON 錯誤，與使用者症狀相容，但沒有當次上游 status/trace，不能宣稱是當次唯一根因。修正將此類例外映射為 `AI_HTTP_UNKNOWN`，本站回安全 JSON 502，結果保留「不明」語義，不自動重播。前端對未來非 JSON 文字檢查只記安全的本站 HTTP status、耗時及粗粒度回應類型（JSON/HTML/TEXT/OTHER/MISSING），不收原始 body、key、PDF 或 cookie。

程式修正與驗證不等於正式部署。ASUS live release 仍需以 `/opt/orderflow/current` 查證。`deploy/upgrade-asus-release.py` 是針對此修正的最小 root gate：只接受固定舊 release 與 SHA 釘選的新來源 tar；依賴與 unit 必須不變，以既有 venv 在新 release 執行測試，檢查無 queued/running 工作，停止 app 後備份 SQLite/PDF，原子切換 symlink 並驗匿名登入頁/受保護 API；失敗嘗試恢復舊 release。它不重匯資料或改 HP 路由。service 重啟會清除記憶體中的使用者 AI key；使用者需在新頁自行重新輸入，不向維護者提供金鑰。正式部署、真實 Google 呼叫和人工驗收分開記錄。

同日另有使用者截圖顯示頁首下方空白。真瀏覽器讀取正式頁面時，初始也短暫只顯頁首，稍後正常出現登入表單；無 console error。已證實的顯示缺陷是 HTML 初始把登入區與工作區都隱藏，而 `initialize()` 遇到 bootstrap 非 JSON、非 401、網路錯誤或逾時時，只更新仍隱藏的工作區，因此畫面可能持續只剩頁首。這說明一條可重現的空白路徑，**不證明使用者當次是哪個 bootstrap 錯誤**。修正讓初始載入說明直接可見；失敗時顯安全訊息與重新檢查按鈕，JS 資源缺失時仍有靜態說明；401 登入、已授權工作區及 API 權限保持分離。本機真瀏覽器以合成 bootstrap 延遲、HTML 502、登入過期、成功與 JS 404 驗證畫面，未動正式服務或使用者資料。
