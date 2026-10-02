# ASUS 整合測試版升級準備

2026-10-02 候選：新增 `/orderflow/integration/`；舊管理頁 `/orderflow/`、診斷頁 `/orderflow/test/` 與既有資料維持可用。這份文件記錄升級閘門與驗證範圍；實際部署結果須在執行後另記錄，不能把此處的測試寫成正式服務已驗收。

## 固定範圍

- 來源為經核對的 OrderFlow PR 合併提交之 `git archive`。升級腳本 `deploy/upgrade-asus-integration-trial.py` 固定要求 ASUS 現行 release 為 `9182d8fd56becfee07651b99c6ab09a9e13e1fa4`，拒絕其他起點及已存在的目標 release。
- 合併後先核對 PR 的 base、head、merge commit 與 Git tree，再製作封存；以 SHA-256 固定封存及腳本，放在 ASUS 操作員私有暫存目錄。執行前再次核對來源、腳本 SHA 與 live release。目標 commit、封存 SHA 與腳本 SHA 由當次實際產物確定，不使用這份文件中的歷史值代替。
- 只切換 ASUS 的 OrderFlow 程式 release；不改 HP Caddy、SSH 轉送、Cloudflare、其他服務、網站登入憑證或現有正式紀錄。服務重啟會清除記憶體內 AI key，使用者需自行在原管理頁重新輸入。
- 新試用 PDF 上限為 6 MiB，整份 JSON 請求必須小於 9 MiB；現行 HP Caddy `/orderflow/` 的 live 限制是 9 MiB。原管理頁 PDF 8 MiB 上限不變。

## 一次性 root 閘門

腳本須由 ASUS 互動終端的管理員，以 root 私有副本執行；不透過聊天傳送 sudo 密碼、登入憑證、PDF 或資料庫。ASUS 的非互動 sudo 尚不可用，操作員未執行前僅能宣稱候選已暫存。

1. 核對目前 symlink、服務運作、既有 API 401、資料目錄及無 queued/running job。檢查封存 SHA、目標 SHA 格式、執行單元與依賴檔同現行版；從現行 release 複製既有 venv，建立只讀候選 release，並以 `orderflow` 身分跑候選測試。
2. 在服務仍運行時以 SQLite backup API 取預檢快照，試跑候選的 schema 初始化；既有 schema、所有舊表列值與 `user_version` 必須不變，四個新試用表必須存在，完整性與外鍵檢查必須通過。
3. 再次確認無待處理 job，停止 `orderflow.service`，確認 `orderflow` 帳號已無程序與 job，並將整個 StateDirectory 複製到 root-only 備份。逐檔核對 raw copy 雜湊，再取停寫後 SQLite 快照及重做遷移試驗。
4. 對 live SQLite 只增加新試用表，逐表核對所有既有列及 schema，對照舊 PDF 等非資料庫檔案雜湊；確認完整性、外鍵及必要新表後才切換 release 並啟動。核對舊管理頁、診斷頁、新試用頁及其靜態資源的位元雜湊、匿名 API 401、CSP、服務 active。

若在停止服務後發生錯誤，腳本會再次停止服務並印出 `HARD STOP`，保留資料、備份與現場，不自動回復舊版或覆寫資料庫。任一 SHA、現行 release、job、備份、SQLite、檔案或路由檢查失敗，須先調查，不盲重跑。這次 root-only 備份不是持續備份或還原演練。

## 分開回報的驗證

- 本機及 ASUS 暫存版：合成舊資料庫含非空本機解析來源與 PDF，驗證新 schema 開啟後舊表列值、PDF 位元、SQLite 完整性和外鍵；測試與來源封存 SHA 要對應最後 PR 版本。
- `tests/fixtures/orderflow-9182-schema.sql` 由現行 `9182d8f` 的 `Store` 空資料庫結構產生，不含使用者資料；`tests/test_upgrade_asus_integration.py` 在其中加入合成 session、管理文件、本機解析來源與 PDF，實際以候選 `Store` 開啟並做上述保留檢查。這仍不能取代 root 閘門對 live 快照的試遷移。
- 升級閘門另以合成狀態注入停服後的遷移失敗，驗證原 `current` 未切換、raw 備份保留且輸出 `HARD STOP`，不自動回復。實際主機發生停止條件時仍須由人核對現場資料與程序。
- ASUS 正式切換後：另核 `current`、服務、loopback 18081、檔案 SHA、新舊靜態頁面、匿名整合 API 401、相鄰路由 404，並確認 HP 轉送與共用 Caddy 未變。
- 公開 HTTPS：核對登入頁、舊管理／診斷／試用頁與匿名保護。正式登入後人工保存、重複確認、主檔衝突、離頁警示、真實 Google 及公司端接受須逐項另列；本次合成與匿名檢查不代表這些項目通過。

試用資料只屬目前瀏覽器會話，不會自動匯入正式管理紀錄；新版尚未整合 Google 辨識，原管理頁的既有入口仍保留。資料共享身份及跨裝置找回需另作產品決策。
