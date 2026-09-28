# 2026-09-28 Google 文字連線診斷

使用者於正式 v0.3.0 網站執行文字連線檢查，安全診斷報告顯示 `Google 文字連線：失敗（BAD_JSON_RESPONSE）`。這代表瀏覽器無法將**本站** `/orderflow/api/key/check` 的回應解析為 JSON；報告沒有該請求 HTTP status、耗時或 Content-Type，因此無法從這份報告判定是哪一層回了非 JSON。另一次固定資料模擬得到 `AI_UNAVAILABLE`，與 Google 請求無關。沒有重播使用者金鑰、讀取程序記憶體或檢視 PDF 內容。

唯讀檢查：ASUS app、HP Caddy、SSH tunnel 均 active，NRestarts=0；該時段三服務沒有可用的請求日誌。ASUS 能解析 `generativelanguage.googleapis.com`，無憑證 GET 完成 TLS 驗證並收到 HTTP 404；這只證明 DNS/TLS/出口可達。匿名 POST 同一路徑經公開 HTTPS 與 HP Caddy 都回本站 JSON 401，不證明帶 session/key 的請求成功。Google 官方 [模型頁](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite) 與 [GenerateContent API](https://ai.google.dev/api/generate-content) 顯示固定 model code 與 REST request 形狀和目前 adapter 一致；尚無真實 Google 成功證據。

已證實的程式缺陷：Google HTTP 讀取若拋 `http.client.HTTPException`（合成 `IncompleteRead`），原 adapter 未捕獲，本站 request handler 直接斷線；本機測試收到 `RemoteDisconnected` 而非 JSON。這條路徑**可能**造成代理的非 JSON 錯誤，與使用者症狀相容，但沒有當次上游 status/trace，不能宣稱是當次唯一根因。修正將此類例外映射為 `AI_HTTP_UNKNOWN`，本站回安全 JSON 502，結果保留「不明」語義，不自動重播。前端對未來非 JSON 文字檢查只記安全的本站 HTTP status、耗時及粗粒度回應類型（JSON/HTML/TEXT/OTHER/MISSING），不收原始 body、key、PDF 或 cookie。

部署前，程式修正與本機驗證尚不能代表正式站已更新。ASUS live release 以 `/opt/orderflow/current` 查證。`deploy/upgrade-asus-release.py` 是針對此修正的最小 root gate：只接受固定舊 release 與 SHA 釘選的新來源 tar；依賴與 unit 必須不變，以既有 venv 在新 release 執行測試，檢查無 queued/running 工作，停止 app 後備份 SQLite/PDF，原子切換 symlink 並驗匿名登入頁/受保護 API；失敗嘗試恢復舊 release。它不重匯資料或改 HP 路由。service 重啟會清除記憶體中的使用者 AI key；使用者需在新頁自行重新輸入，不向維護者提供金鑰。正式部署、真實 Google 呼叫和人工驗收分開記錄。

同日另有使用者截圖顯示頁首下方空白。真瀏覽器讀取正式頁面時，初始也短暫只顯頁首，稍後正常出現登入表單；無 console error。已證實的顯示缺陷是 HTML 初始把登入區與工作區都隱藏，而 `initialize()` 遇到 bootstrap 非 JSON、非 401、網路錯誤或逾時時，只更新仍隱藏的工作區，因此畫面可能持續只剩頁首。這說明一條可重現的空白路徑，**不證明使用者當次是哪個 bootstrap 錯誤**。修正讓初始載入說明直接可見；失敗時顯安全訊息與重新檢查按鈕，JS 資源缺失時仍有靜態說明；401 登入、已授權工作區及 API 權限保持分離。本機真瀏覽器以合成 bootstrap 延遲、HTML 502、登入過期、成功與 JS 404 驗證畫面，未動正式服務或使用者資料。

## 2026-09-29 ASUS 升級預備階段失敗

首次 code-only 升級只輸出 `CalledProcessError`，且在停止服務前的 staging 階段結束；`current` 仍指向 `a6e619f`，ASUS app 未重啟，正式 SQLite/PDF 未變動。舊腳本丟棄子程序輸出，也未記錄失敗 phase，因此無法從該次記錄精確指定是哪個命令失敗。

在 ASUS 以操作員帳號、原候選版來源 tar、同一 Python 3.14 `tarfile.extractall(filter="data")` 與 `umask 077` 重現：來源中的 `orderflow/` 與 `tests/` 子目錄實際變成 `0700`，普通使用者無法進入；原腳本只移除 group/other 寫入權，並只把 staging 根目錄改成 `0755`。這足以使後續以 `orderflow` 服務帳號執行的原始碼可讀性檢查失敗，與已觀察的停機前失敗相符；因原 gate 缺少 phase，仍屬高度可信根因推論。候選 tar 共 34 個條目，複製的 venv 共 91 個一般檔案，未發現 DB/PDF/key/state 類項目；正式 `/var/lib/orderflow` 仍為 `0700`。

修正對僅含 git archive 與既有 venv 的 release staging 執行 `chmod -R a+rX,go-w`，讓程式子目錄可走訪、原始碼可讀，維持他人不可寫；服務資料目錄不在此樹中。升級腳本的子程序失敗改為固定 phase 與退出碼，不回傳命令輸出、原始錯誤本文或敏感資料。合成 tar/umask 回歸測試與 Python/Node 測試通過。這次失敗未執行到資料備份、切換及回復路徑；後續正式 gate 的結果見下節。

## 2026-09-29 正式部署與無憑證核對

使用者在 ASUS 互動終端機執行 SHA 釘選的一次性 root gate，回報 `ASUS code-only upgrade and protected loopback health: PASS`、runtime release `eb2f928ce213a6b4b23dca6b04f095f5adc98bb9`、root-only 備份位置 `/var/backups/orderflow/before-google-text-fix-eb2f928ce213`。腳本回報 SQLite/PDF/session 保留；非特權唯讀核對無法進入 root 備份，因此本紀錄不把備份內容視為獨立複驗。服務重啟已清除記憶體 AI key，使用者需自行在頁面重填。

部署後唯讀檢查：ASUS `current` 指向上述 release，`orderflow.service` active/enabled、Result=success、NRestarts=0，起始時間 2026-09-28 23:41:05 UTC，`/var/lib/orderflow` 仍為 `0700 orderflow:orderflow`。ASUS loopback 登入頁 200、匿名 bootstrap 401 JSON `AUTH_REQUIRED`；runtime HTML/JS 的 SHA-256 與該 release 原始檔一致。HP Caddy、cloudflared、`orderflow-asus-tunnel` active，tunnel enabled/NRestarts=0，舊 HP app inactive/disabled；Caddy 設定 SHA-256 仍為 `fe0e95aa488cfb73a8d801bc332e40e866a65a32454e11f69979293a4b961be8`。HP 本機 Caddy 127.0.0.1:18080 經相同 Host 與 HTTPS 轉送標頭回首頁 200、登入頁 200、匿名 bootstrap 401 JSON，沒有 Basic challenge，相鄰前綴 404。

公開 HTTPS 的首頁 200、登入 HTML/JS/CSS 均 200 且內容逐位元等於 `eb2f928` 原始檔，匿名 bootstrap 401 JSON `AUTH_REQUIRED`，相鄰前綴 404。main 另以真瀏覽器唯讀觀察到初始可見載入說明，隨後轉成登入欄位。這些證據只涵蓋匿名路徑與資源版本；正式登入後流程、真實 Google key、LINE 內建瀏覽器與人工驗收仍未完成。PR #3 已推送且 GitGuardian 通過，但因自動審核拒絕寫入 main，當次部署後尚未合併；Git 狀態與 runtime 狀態分開追蹤。
