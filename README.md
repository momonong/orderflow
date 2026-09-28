# OrderFlow 公司端可行性驗證頁

這是本機工程 PoC，用來逐段檢查同站網頁／API、PDF 傳輸完整性與**模擬** AI 工作流程。它不是正式訂單管理系統，也沒有真實外部 AI 呼叫。階段規格與授權界線見 [docs/feasibility-v0.1.md](docs/feasibility-v0.1.md)。

## 啟動

需要 Python 3.12+ 與 uv。從 repo 根目錄執行：

```bash
uv sync --locked
uv run --locked python -m orderflow.app --port 8765
```

瀏覽 `http://127.0.0.1:8765/orderflow/`。預設只綁 loopback，程式會拒絕其他 bind host 與 Host header；不要把此版本直接開到 LAN／公網。資料預設放在 repo 的 `.local-data/`，不納入 Git。可用 `--data-dir` 指向另一個本機目錄。請只上傳去識別測試 PDF，容量上限 8 MiB。文件持久保存，**目前沒有保留期限或自動清理功能**；使用前應確認目錄權限與磁碟空間。

## 使用流程

1. 按「執行基本檢查」驗證同站 JSON 往返及固定資料表格。
2. 選擇 PDF，檢查大小，按「確認並上傳」，再確認瀏覽器對話框。瀏覽器與後端分別計算 SHA-256；伺服器在 5 秒限時子程序內使用 pypdf 驗證 PDF 基本結構，回執大小與 hash 一致才顯示收件完整；後端另提供可靠取得的頁數。這不是惡意檔案掃描或內容真實性驗證。
3. 按「開始模擬辨識」。可選成功、AI 故障、逾時結果不明、結果格式錯誤。重複點「開始」查回同一工作；只有「明確重新辨識」建立新嘗試。
4. 重新整理會依本機 HttpOnly session cookie 找回本次瀏覽器的文件與工作。工作識別不能單獨取得他人資料。報告與辨識結果分開；剪貼簿不能用時可選取報告手動複製。

## 本機驗證

```bash
uv run --locked python -m unittest discover -s tests -v
node tests/test_report.cjs
node --check web/app.js
```

測試使用暫時 SQLite、合成 PDF 與 loopback HTTP，覆蓋子路徑、大小與 hash、無效 PDF、並發同鍵重送、session 隔離、模擬故障、逾時、重啟後結果不明與報告複製 fallback。沒有測試真實外部 AI、公開 HTTPS 或公司瀏覽器。

## 架構與安全界線

- 單一 Python HTTP 應用提供 `/orderflow/` 頁面、CSS、JS 與 API。SQLite 保存 session、文件、工作與步驟；PDF 存在 0700 目錄，檔案模式 0600。無下載 PDF 的 API。
- 本機 session cookie 為隨機秘密，資料庫只保存其 SHA-256；工作／文件依 session 隔離。此機制不等於正式登入。網頁 API 使用同站 cookie 與自訂請求標頭，不提供 CORS。
- 辨識以 `AIAdapter` 協定隔離；目前只有 `MockAdapter` 連到工作流程，`ExternalAdapter` 明確回覆未設定。最終供應商、model、憑證來源、外部請求／回應契約與真實呼叫授權待決，沒有 API key 會傳到前端。
- 上傳與工作等待有界。服務重啟時將未完成工作標為 `SERVER_RESTART`／結果不明，不自動重播。外部 exactly-once、斷點續傳、離線與重啟續跑都未提供。
- 這個本機 PoC 沒有正式登入、流量／配額治理、備份或資料保留／清理政策。公開使用前，需由 main／使用者確認安全、HTTPS 入口及資料政策；本階段不部署。
- 若整個網站無法載入，頁面無法自行產生診斷報告。請由人提供錯誤截圖與發生時間；仍不能單憑此判定 DNS、代理或防火牆根因。
