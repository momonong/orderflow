# OrderFlow 物流管理草稿與診斷測試

管理首頁可直接上傳 PDF、明確啟動 Google 辨識、人工編修並儲存品項草稿；目前沒有正式訂單、出貨或實體庫存紀錄。原有診斷測試移至 `/orderflow/test/`，仍提供固定資料模擬與真實辨識。兩種用途的文件和工作彼此隔離。詳細契約與待決事項見 [管理介面第一增量](docs/management-shell-phase1.md)。

## 本機啟動

需要 Python 3.12+ 與 uv：

```bash
uv sync --locked
uv run --locked python -m orderflow.app --port 8765 --data-dir .local-data --auth-file /path/to/local-caddy-auth.caddy
```

`--auth-file` 需為含單一 `orderflow` 帳號與 bcrypt hash 的 Caddy `basic_auth` 區塊；缺少或格式錯誤會拒絕啟動。正式服務從 systemd `LoadCredential` 讀取同一 hash，不讀明文密碼檔；ASUS 正式 unit 為 `deploy/orderflow-asus.service`。瀏覽管理首頁 `http://127.0.0.1:8765/orderflow/`，或診斷頁 `http://127.0.0.1:8765/orderflow/test/`。預設只綁 `127.0.0.1`。頁面、靜態資源與 API 都使用 `/orderflow/` 前綴。上傳 PDF 上限 8 MiB；後端核對大小與 SHA-256，並以有時限的子程序檢查 PDF 基本結構。這不是惡意檔案掃描。

## 診斷測試使用方式

1. 先使用提供的網站密碼登入，再檢查網站連線，只用沒有客戶或個人資料、且已獲准外傳的測試 PDF。
2. 選檔，確認後上傳；PDF 保存在網站主機的資料目錄，不只在瀏覽器。
3. 可先按「模擬測試」檢查畫面；如需真正辨識，設定自己的 AI Studio key，可先做文字連線檢查，再另外確認把 PDF 送給 Google。文字成功不代表 PDF 辨識成功。
4. 核對辨識品項並複製診斷報告。報告不含 API key、PDF 內容、檔名或辨識品項。

金鑰只保存在單一服務程序的記憶體，設定 15 分鐘後失效；清除或程序重啟後需重新輸入。Google AI Studio API key 與網站登入密碼不同；兩個欄位分開，金鑰欄位初次顯示時清空，返回頁面時也會清除未主動輸入的內容；使用者正在輸入的內容不會因頁面狀態更新而清除。前後端只檢查輸入是否為可安全傳入 HTTP 標頭的單行可列印內容、合理長度與非明顯網址，不推定 Google 金鑰前綴或真實有效性；只有使用者明確按診斷頁「先測文字連線」才會向 Google 驗證，且可能使用 API 額度。已開始的 Google 請求不能撤回；結果不明時不自動重送。session cookie 為隨機秘密，資料庫只保存其雜湊；金鑰不寫入資料庫、cookie、報告或日誌。網站登入有效期 8 小時；登出會換成無權限 cookie，重新登入同一瀏覽器可取回該 session 的文件與工作。清除 cookie 或更換瀏覽器不保證取回。這是單一共用測試帳號，不是正式多使用者授權。

## 公開測試與 ASUS 部署

2026-09-28 已將應用及唯一可寫的 SQLite/PDF 資料遷至 ASUS，公開 URL 維持 `https://momonong.me/orderflow/`。HP 保留 Cloudflare Tunnel、Caddy、公開首頁及其他服務；HP 舊 `orderflow.service` 已停用，原始資料及停寫備份保留。ASUS app 僅監聽 `127.0.0.1:18081`，HP Caddy 經本機 `127.0.0.1:18082` 的釘選 SSH local forward 連到 ASUS；網站入口改用應用表單登入。主機與資料驗證詳見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)；切換及回復流程見 `selfhost-servers/docs/orderflow-session-rollout.md`。

2026-09-29 金鑰欄位修正已部署：ASUS runtime release 為 `7d451d9ec857dbd6f8174adad30cba1d088a9110`；GitHub `main` 合併提交為 `86588dcfc644bbda636a35895e411858632727f2`，兩者程式樹相同。root gate 回報備份與資料保留通過；獨立唯讀核對的服務及公開路由結果見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)。ASUS root-only 私鑰、HP 原憑證與備份均保留；遷移過程中的操作員可讀 credential 密文已於成功匯入後刪除。

在公開模式下，應用使用 `--public-origin https://momonong.me`，只接受相符的 Host，寫入請求須有相符 Origin，session cookie 使用 `Secure`。匿名只可讀登入頁與靜態資源；API 資料與操作均須登入。應用不信任任意客戶端代理標頭，也不接受前端指定任意 Google URL 或模型 ID。需登入的健康檢查為 `/orderflow/api/health`；無 session 回傳 401。Caddy 已停止對 `/orderflow/` 使用 Basic Auth；應用表單登入保護 API 與資料。

公開模式額外限制：每個 session 最多 20 份 PDF、整個資料目錄的正式文件合計最多 128 MiB、每份文件最多 10 次辨識工作；同時最多 2 個上傳檢查及 1 個 Google 辨識。超過限制會拒絕新請求，不自動刪除既有文件。資料預設持久保存；HP 遷移時有一次性 root-only 備份，但**尚未建立 ASUS 持續備份、保留期限或自動清理政策**；服務負責者需核對磁碟與資料目錄。ASUS unit 使用靜態 `orderflow` 系統帳號及 0700 `StateDirectory`；程式碼由 root 持有，登入 bcrypt hash 透過 systemd credential 唯讀交給應用。HP v0.2 的首次安裝腳本與 unit 只留在 Git 歷史及主機原始備份中；正式分支不提供重跑入口。不得以過時的 HP 資料直接回復目前 ASUS 服務。

## 驗證

```bash
uv run --locked python -m unittest discover -s tests -v
node tests/test_report.cjs
node tests/test_guided_flow.cjs
node tests/test_startup.cjs
node tests/test_management_ui.cjs
node --check web/app.js
node --check web/manage.js
```

測試涵蓋同站 API、PDF 邊界及持久化、公開來源限制、金鑰隔離/清除/重啟、固定模型請求格式、Google 錯誤碼、模擬與可控 stub 工作。沒有真實金鑰時，**不能宣稱 Google API 或 PDF 辨識實測通過**；公司瀏覽器及資料外傳許可也需由使用者確認。
