# OrderFlow PDF 測試頁

這是供少量受邀使用者檢查 PDF 上傳與辨識流程的測試服務，不是正式訂單管理系統。頁面提供固定資料的模擬測試，也可由使用者自行輸入 Google AI Studio API key，明確確認後把**測試 PDF**送到 Google Gemini 3.1 Flash-Lite 辨識。真實模型輸出需要人工核對；未提供金鑰時不會呼叫 Google，也不會改用模擬結果冒充辨識結果。

## 本機啟動

需要 Python 3.12+ 與 uv：

```bash
uv sync --locked
uv run --locked python -m orderflow.app --port 8765 --data-dir .local-data
```

瀏覽 `http://127.0.0.1:8765/orderflow/`。預設只綁 `127.0.0.1`。頁面、靜態資源與 API 都使用 `/orderflow/` 前綴。上傳 PDF 上限 8 MiB；後端核對大小與 SHA-256，並以有時限的子程序檢查 PDF 基本結構。這不是惡意檔案掃描。

## 使用方式

1. 檢查網站連線，只用沒有客戶或個人資料、且已獲准外傳的測試 PDF。
2. 選檔，確認後上傳；PDF 保存在網站主機的資料目錄，不只在瀏覽器。
3. 可先按「模擬測試」檢查畫面；如需真正辨識，設定自己的 AI Studio key，可先做文字連線檢查，再另外確認把 PDF 送給 Google。文字成功不代表 PDF 辨識成功。
4. 核對辨識品項並複製診斷報告。報告不含 API key、PDF 內容、檔名或辨識品項。

金鑰只保存在單一服務程序的記憶體，設定 15 分鐘後失效；清除或程序重啟後需重新輸入。已開始的 Google 請求不能撤回；結果不明時不自動重送。session cookie 為隨機秘密，資料庫只保存其雜湊；金鑰不寫入資料庫、cookie、報告或日誌。此機制不是正式登入。

## 公開測試邊界

正式公開入口的路由、認證與操作由 `selfhost-servers` 專案管理。此服務仍只綁 loopback；使用 `--public-origin https://momonong.me` 時，只接受相符的 Host，寫入請求須有相符 Origin，session cookie 增加 `Secure`。入口需對 `/orderflow` 與 `/orderflow/*` 執行獨立認證並保留前綴；不得讓匿名用戶直連上傳 API。應用不信任任意客戶端提供的代理標頭，也不從前端接受任意 Google URL 或模型 ID。健康檢查為 `/orderflow/api/health`。

公開模式額外限制：每個 session 最多 20 份 PDF、整個資料目錄的正式文件合計最多 128 MiB、每份文件最多 10 次辨識工作；同時最多 2 個上傳檢查及 1 個 Google 辨識。超過限制會拒絕新請求，不自動刪除既有文件。資料預設持久保存，**尚未建立保留期限、備份或自動清理政策**；服務負責者需核對磁碟與資料目錄。應用 service unit 候選檔見 `deploy/orderflow.service`：以 systemd `DynamicUser` 專屬身份執行、`StateDirectory` 保存 0700 資料，程式碼在 root 擁有的 `/opt/orderflow/current` 唯讀使用，服務禁止讀取一般使用者家目錄。實際入口與 systemd 狀態依部署後驗證紀錄判定。

## 驗證

```bash
uv run --locked python -m unittest discover -s tests -v
node tests/test_report.cjs
node tests/test_guided_flow.cjs
node --check web/app.js
```

測試涵蓋同站 API、PDF 邊界及持久化、公開來源限制、金鑰隔離/清除/重啟、固定模型請求格式、Google 錯誤碼、模擬與可控 stub 工作。沒有真實金鑰時，**不能宣稱 Google API 或 PDF 辨識實測通過**；公司瀏覽器及資料外傳許可也需由使用者確認。
