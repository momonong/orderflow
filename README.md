# OrderFlow 物流管理草稿與診斷測試

本版管理首頁依使用者單檔原型提供儀表板、採購單與發票 PDF 上傳、人工確認的訂單／發票紀錄、發票金額統計、訂購與開票數量對照及 CSV 匯出。Google 辨識仍由網站後端執行，必須明確確認付費請求；已確認資料保存在 SQLite，原 PDF 與 AI 工作結果保留。發票不等於實際出貨，數量差不等於待出貨或實體庫存。這一階段已於 2026-09-29 部署於 ASUS；正式站的唯讀驗證與尚待人工驗收的範圍見下方部署紀錄。新契約見 [原型繼承與採購單／發票管理](docs/prototype-management-phase2.md)，既有未分類品項草稿與診斷流程見 [管理介面第一增量](docs/management-shell-phase1.md)。

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

2026-09-29 原型管理頁已部署：GitHub [PR #7](https://github.com/momonong/orderflow/pull/7) 合併提交 `0ab2b1c80cf0c7623bd05ebb7b75a88ff82415a9`，ASUS runtime 為 `ee41445565a73ea7e0b3444f0bb1bfad5e6ddd2a`，兩者 Git tree 相同。使用者執行的一次性 root gate 回報 SQLite/PDF/session 保留與升級前備份 `/var/backups/orderflow/before-prototype-records-ee41445565a7`；非特權唯讀核對的服務、公開路由與限制見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)。此備份不是持續備份；真實 Google 辨識與使用者工作流程仍待驗收。

2026-09-29 日期與保存錯誤修正已部署：GitHub [PR #9](https://github.com/momonong/orderflow/pull/9) 合併提交 `9abdfd8e4f6999f281206644c32519298192851d`，ASUS runtime 為 `37a404fcea6331b3be984fdfc9e3756c56cc54bd`，兩者 Git tree 相同。一次性 root gate 回報備份及 SQLite/PDF/session 保留通過；獨立唯讀核對的服務、靜態檔與公開入口見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)。此版只修正日期輸入與儲存錯誤定位，不變更 Google 辨識逾時或重試語義；人工驗收仍待完成。

2026-09-30 管理頁安全錯誤資訊複製已部署：GitHub [PR #11](https://github.com/momonong/orderflow/pull/11) 合併提交與 ASUS runtime 均為 `24bd38ca849159a21a1c692b7d9db8d62e8cffe3`。可從既有結果不明或失敗的辨識工作複製僅含白名單欄位的摘要，不需重新送出付費請求。一次性 root gate 回報備份及 SQLite/PDF/session 保留通過；正式站靜態檔、服務與公開入口的唯讀核對見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)。辨識逾時原因與重試語義未變，正式站登入後操作及人工驗收仍待完成。

2026-10-02 瀏覽器端採購憑單解析狀態核對：ASUS runtime 已指向本機候選提交 `fbd9087425f30f4807c387d14c6229d6e796126a`，公開 [OrderFlow](https://momonong.me/orderflow/) 登入頁及同源 PDF.js/Worker 靜態資源可用。該功能提交尚未推送或合併到 GitHub；正式登入後的合成 PDF 保存、跨裝置人工試用及這次 root-only 備份內容仍待核對。詳見 [ASUS 部署紀錄](docs/asus-deployment-2026-09-28.md)與[本機解析契約](docs/browser-local-pdf-parser.md)。

在公開模式下，應用使用 `--public-origin https://momonong.me`，只接受相符的 Host，寫入請求須有相符 Origin，session cookie 使用 `Secure`。匿名只可讀登入頁與靜態資源；API 資料與操作均須登入。應用不信任任意客戶端代理標頭，也不接受前端指定任意 Google URL 或模型 ID。需登入的健康檢查為 `/orderflow/api/health`；無 session 回傳 401。Caddy 已停止對 `/orderflow/` 使用 Basic Auth；應用表單登入保護 API 與資料。

公開模式額外限制：每個 session 最多 20 份 PDF、整個資料目錄的正式文件合計最多 128 MiB、每份文件最多 10 次辨識工作；同時最多 2 個上傳檢查及 1 個 Google 辨識。超過限制會拒絕新請求，不自動刪除既有文件。資料預設持久保存；HP 遷移時有一次性 root-only 備份，但**尚未建立 ASUS 持續備份、保留期限或自動清理政策**；服務負責者需核對磁碟與資料目錄。ASUS unit 使用靜態 `orderflow` 系統帳號及 0700 `StateDirectory`；程式碼由 root 持有，登入 bcrypt hash 透過 systemd credential 唯讀交給應用。HP v0.2 的首次安裝腳本與 unit 只留在 Git 歷史及主機原始備份中；正式分支不提供重跑入口。不得以過時的 HP 資料直接回復目前 ASUS 服務。

## 驗證

```bash
uv run --locked python -m unittest discover -s tests -v
node tests/test_report.cjs
node tests/test_guided_flow.cjs
node tests/test_startup.cjs
node tests/test_management_ui.cjs
node tests/test_management_error_copy.cjs
node tests/test_records_ui.cjs
node --check web/app.js
node --check web/manage.js
```

測試涵蓋同站 API、PDF 邊界及持久化、公開來源限制、金鑰隔離/清除/重啟、固定模型請求格式、Google 錯誤碼、模擬與可控 stub 工作。沒有真實金鑰時，**不能宣稱 Google API 或 PDF 辨識實測通過**；公司瀏覽器及資料外傳許可也需由使用者確認。
