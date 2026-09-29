# 2026-09-28 ASUS 正式部署紀錄

- 環境：HP／ASUS Ubuntu 26.04.1 LTS x86_64；HP Cloudflare Tunnel → Caddy → HP loopback SSH forward `127.0.0.1:18082` → ASUS app loopback `127.0.0.1:18081`。公開 URL 為 `https://momonong.me/orderflow/`。
- ASUS runtime release：`a6e619fca9c23bd7a36e6ed5bfde346a97b485b0`；release manifest 與該來源一致，ASUS Python 3.14.4 的 29 項測試通過。部署後僅修復 release 頂層目錄 mode 0700→0755；程式內容未改。`/opt/orderflow/current` 仍指向該 release。安裝器原始碼後續修正為 `7e38a5d939daffc54f17b307a54835a4cdde3ab7`，僅供下次部署。
- HP 停用舊 `orderflow.service` 後建立 root-only raw/snapshot 備份，位置 `/var/backups/orderflow/asus-migration-11nxm77y`。停寫匯出含 session 4、document 2、job 2、pending 0；SQLite snapshot SHA-256 `8ab65fce572df673e10ec187215bd95488dcfd4580c05faca55d41e8c5f3a6ab`。ASUS 匯入前核對 tar、snapshot、加密 credential、公鑰與每份 PDF；匯入後對 session/document/job 關係及 PDF SHA 驗證通過。這些數字是匯入時點，不代表後續寫入後總數。
- ASUS root Gate2 首次啟動因複製的 release 頂層仍為 0700，systemd 回 `200/CHDIR`。只將該頂層改為 0755，確認 service 帳號可進入，未重匯或重設資料庫；服務後續啟動並通過資料核對。來源安裝器已補上 mode 與 service 帳號 preflight 回歸測試。
- HP root Gate3 核對舊 token 換新 cookie、舊 PDF/job 可讀，切換 Caddy 並透過 HP 本機 Caddy 測新合成 PDF、mock job、登出；未呼叫 Google。HP 舊 app 維持 inactive/disabled，ASUS app active/enabled、NRestarts=0。HP Caddy 正式 SHA-256 `fe0e95aa488cfb73a8d801bc332e40e866a65a32454e11f69979293a4b961be8`；HP tunnel active/enabled、NRestarts=0。
- 管理桌機經**真實公開 HTTPS** 獨立核對首頁 200、登入頁 200、JS/CSS 200、匿名 bootstrap 401 `AUTH_REQUIRED` 且無 Basic Auth challenge、前綴邊界 404、HTTP→HTTPS 308。公開 HTTPS 的**登入後** PDF/mock/logout 尚未獨立跑完；HP root Gate3 的相同操作經本機 Caddy 成功，證據範圍不同。
- 真實 LINE 內建瀏覽器選檔／複製、Google 文字與 PDF 呼叫、辨識品質與人工驗收均未驗證。ASUS 持續備份與保留政策尚未建立；一次性 HP 備份不可當作後續 ASUS 寫入備份。

不要重啟 HP 舊服務或把 Caddy 指回舊資料。回復需先對帳 ASUS 新寫入，詳見 `selfhost-servers/docs/orderflow-session-rollout.md`。

## 2026-09-29 金鑰欄位修正部署

- GitHub [PR #5](https://github.com/momonong/orderflow/pull/5) 已合併至 `main`，merge commit `86588dcfc644bbda636a35895e411858632727f2`；ASUS 使用 PR head release `7d451d9ec857dbd6f8174adad30cba1d088a9110`，兩者 Git tree 同為 `5872427ba010b6e08dacbe0c1bc90f10f7bf1419`。管理介面本體屬於先前已合併的 PR #4，本次只修正 Google key 輸入生命週期與安全提示。
- 操作者執行的一次性 ASUS root gate 回報：候選 tar 與腳本 SHA-256 均通過；`ASUS key-input upgrade and protected loopback health: PASS`；升級前備份為 `/var/backups/orderflow/before-key-input-fix-7d451d9ec857`；SQLite、PDF、session 保留，程序重啟後記憶體內 AI key 清除。非特權核對無法讀取 root-only 備份內容，以上資料保留結論來自 gate 回報。
- 隨後由非特權帳號獨立核對：ASUS `/opt/orderflow/current` 指向 `releases/7d451d9ec857dbd6f8174adad30cba1d088a9110`，`orderflow.service` active/enabled、`NRestarts=0`，僅監聽 `127.0.0.1:18081`，`/var/lib/orderflow` 為 `0700 orderflow:orderflow`。HP `orderflow-asus-tunnel`、Caddy、cloudflared 均 active/enabled、`NRestarts=0`；HP 舊 `orderflow.service` inactive/disabled；轉送僅監聽 `127.0.0.1:18082`。HP live Caddyfile SHA-256 仍為 `fe0e95aa488cfb73a8d801bc332e40e866a65a32454e11f69979293a4b961be8`。
- 公開 HTTPS 唯讀核對：`/orderflow/` 與 `/orderflow/test/` 回 200，HTML、管理與診斷 JS/CSS 的 SHA-256 均與 ASUS release 相同；匿名 `/orderflow/api/bootstrap` 和 `/orderflow/api/management/bootstrap` 回 401 `AUTH_REQUIRED`，沒有 Basic Auth challenge；首頁 `/` 回 200，`/orderflow-other/` 回 404，`/orderflow/test` 回 308 並導向 `/orderflow/test/`。
- 本機以與 ASUS 靜態資源逐位元相同的程式碼及**合成帳號/金鑰**核對兩頁：金鑰欄位無預設 `value`、初始 `readonly`，與網站密碼欄位名稱分開；貼入網址被本地拒絕，合成金鑰可設定、清除及登出。沒有使用真實金鑰、上傳真實 PDF 或再次呼叫 Google。第三方密碼管理器可能稍後填入欄位，使用者實際 Chrome 尚待驗收；本次不能宣稱完整物流功能或人工接受。
- ASUS 操作員私有 staging `/home/morris/orderflow-key-input-7d451d9` 與 root 私有 gate 目錄尚未清理；保留候選封存、腳本及備份供交接。ASUS 持續備份與保留政策仍未建立；這次一次性 root 備份不等於持續備份。

## 2026-09-29 原型管理頁部署

- GitHub [PR #7](https://github.com/momonong/orderflow/pull/7) 已合併至 `main`，merge commit `0ab2b1c80cf0c7623bd05ebb7b75a88ff82415a9`；ASUS 使用 PR head release `ee41445565a73ea7e0b3444f0bb1bfad5e6ddd2a`，兩者 Git tree 同為 `c06de256905cf6388be36c2002339fa556783543`。變更為六頁採購單／發票管理、人工確認記錄、按幣別報表與 CSV；發票不等於實際出貨，差額不等於庫存。
- 使用者在 ASUS 互動終端執行的一次性 root gate 回報四筆來源／root 副本 SHA-256 `OK`，`ASUS prototype-record upgrade and protected loopback health: PASS`；升級前備份 `/var/backups/orderflow/before-prototype-records-ee41445565a7`，SQLite、PDF、session 保留，程序重啟後記憶體 AI key 清除。非特權帳號無法讀取 root-only 備份內容；資料保留結論來自 gate 回報，不冒充獨立備份還原驗證。
- 非特權獨立核對：ASUS `/opt/orderflow/current` 指向 `releases/ee41445565a73ea7e0b3444f0bb1bfad5e6ddd2a`，app active/enabled、NRestarts=0，僅監聽 `127.0.0.1:18081`；`/var/lib/orderflow` 為 `0700 orderflow:orderflow`。部署檔 `web/index.html`、`web/manage.js`、`web/manage.css` 的 SHA-256 與 Git 來源一致。HP 舊 app inactive/disabled，Tunnel/Caddy/cloudflared active，轉送僅監聽 `127.0.0.1:18082`；HP Caddy 設定 SHA-256 仍為 `fe0e95aa488cfb73a8d801bc332e40e866a65a32454e11f69979293a4b961be8`，未修改共用入口。
- 公開 HTTPS 唯讀核對：首頁 `/`、管理 `/orderflow/`、診斷 `/orderflow/test/` 均 200；管理頁包含新發票與數量對照區，公開 `manage.js`／`manage.css` 與 release 檔逐位元相同；匿名 `/orderflow/api/management/bootstrap` 回 401 JSON `AUTH_REQUIRED`；未知相鄰路由 404，`/orderflow` 回 308。這只證明匿名路由與靜態內容，沒有登入後正式環境操作、真實 Google 請求、真實客戶 PDF 或使用者人工接受。合成多品項 HTTP→保存→重啟→報表／CSV 與 Chrome 真 CSS 390px 已在候選提交驗證；PR 無 GitHub CI 工作，不能稱 CI 通過。
- ASUS 操作員 staging `/home/morris/.staging-prototype-ee414455`、root gate 目錄及 root-only 備份保留供交接，尚未清理。若新版已產生 typed 文件／記錄，不可盲退不理解此資料的舊 runtime；應先停寫對帳並按部署腳本的 typed-write 閘門處理。ASUS 持續備份、保留期限與自動清理政策仍未建立。
