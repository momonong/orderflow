# 2026-09-28 ASUS 正式部署紀錄

- 環境：HP／ASUS Ubuntu 26.04.1 LTS x86_64；HP Cloudflare Tunnel → Caddy → HP loopback SSH forward `127.0.0.1:18082` → ASUS app loopback `127.0.0.1:18081`。公開 URL 為 `https://momonong.me/orderflow/`。
- ASUS runtime release：`a6e619fca9c23bd7a36e6ed5bfde346a97b485b0`；release manifest 與該來源一致，ASUS Python 3.14.4 的 29 項測試通過。部署後僅修復 release 頂層目錄 mode 0700→0755；程式內容未改。`/opt/orderflow/current` 仍指向該 release。安裝器原始碼後續修正為 `7e38a5d939daffc54f17b307a54835a4cdde3ab7`，僅供下次部署。
- HP 停用舊 `orderflow.service` 後建立 root-only raw/snapshot 備份，位置 `/var/backups/orderflow/asus-migration-11nxm77y`。停寫匯出含 session 4、document 2、job 2、pending 0；SQLite snapshot SHA-256 `8ab65fce572df673e10ec187215bd95488dcfd4580c05faca55d41e8c5f3a6ab`。ASUS 匯入前核對 tar、snapshot、加密 credential、公鑰與每份 PDF；匯入後對 session/document/job 關係及 PDF SHA 驗證通過。這些數字是匯入時點，不代表後續寫入後總數。
- ASUS root Gate2 首次啟動因複製的 release 頂層仍為 0700，systemd 回 `200/CHDIR`。只將該頂層改為 0755，確認 service 帳號可進入，未重匯或重設資料庫；服務後續啟動並通過資料核對。來源安裝器已補上 mode 與 service 帳號 preflight 回歸測試。
- HP root Gate3 核對舊 token 換新 cookie、舊 PDF/job 可讀，切換 Caddy 並透過 HP 本機 Caddy 測新合成 PDF、mock job、登出；未呼叫 Google。HP 舊 app 維持 inactive/disabled，ASUS app active/enabled、NRestarts=0。HP Caddy 正式 SHA-256 `fe0e95aa488cfb73a8d801bc332e40e866a65a32454e11f69979293a4b961be8`；HP tunnel active/enabled、NRestarts=0。
- 管理桌機經**真實公開 HTTPS** 獨立核對首頁 200、登入頁 200、JS/CSS 200、匿名 bootstrap 401 `AUTH_REQUIRED` 且無 Basic Auth challenge、前綴邊界 404、HTTP→HTTPS 308。公開 HTTPS 的**登入後** PDF/mock/logout 尚未獨立跑完；HP root Gate3 的相同操作經本機 Caddy 成功，證據範圍不同。
- 真實 LINE 內建瀏覽器選檔／複製、Google 文字與 PDF 呼叫、辨識品質與人工驗收均未驗證。ASUS 持續備份與保留政策尚未建立；一次性 HP 備份不可當作後續 ASUS 寫入備份。

不要重啟 HP 舊服務或把 Caddy 指回舊資料。回復需先對帳 ASUS 新寫入，詳見 `selfhost-servers/docs/orderflow-session-rollout.md`。
