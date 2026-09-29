# 管理介面第一增量（2026-09-29）

## 已確認範圍

`/orderflow/` 是受既有網站登入保護的管理工作區，可直接上傳 PDF、設定暫存的 Google AI Studio key、逐次確認並啟動真正辨識、人工編修品項及明確儲存草稿。`/orderflow/test/` 保留原診斷流程。診斷文件與工作不轉作管理草稿，原始 AI 工作結果不可編輯。管理草稿只代表可核對的品項，**不代表正式訂單、出貨、待出貨或實體庫存**。

管理資料仍以現有 session 為擁有者。共用網站密碼不提供個別人員身份或跨裝置取回能力；cookie 遺失後的資料取回尚未設計。金鑰仍僅在單一服務程序記憶體保存 15 分鐘，登入輪替、登出及重啟會清除。介面不在載入或重連後自動呼叫 Google，查詢失敗或結果不明也不自動重送。

## 資料與 API 契約

- `documents.purpose` 僅允許 `diagnostic` 或 `management`；遷移時舊文件成為 `diagnostic`。用途由端點選定，客戶端不得在內容中指定。
- 舊 `/api/bootstrap`、`/api/documents`、`/api/jobs`、`/api/jobs/{id}` 限診斷用途；管理版是 `/api/management/bootstrap`、`/api/management/documents`、`/api/management/jobs`、`/api/management/jobs/{id}`。跨用途或跨 session 的工作操作回 404；兩種上傳請求識別空間分離。
- `PUT /api/management/drafts/{source_job_id}` 接受 `{"revision": 0, "rows": [{"description": "...", "quantity": 1}]}`。來源必須是該 session 成功的管理真實辨識工作。初次儲存 `revision=0` 產生修訂 1；之後以回傳修訂值更新。相同內容的重送回傳既有草稿，過期修訂且內容不同回 `DRAFT_VERSION_CONFLICT`。每列品項 1–200 字、數量 0–1,000,000,000 整數、總列數 1–100；伺服器驗證。草稿儲存在 SQLite，未按儲存的前端編修不保存。
- AI HTTP 400 只留安全的上游狀態碼與 `INVALID_ARGUMENT`、`FAILED_PRECONDITION`、`UNCLASSIFIED` 類別；不保存 Google 原文、API key、PDF 或客戶資料到診斷訊息。應用回應加 `X-Orderflow-Origin: app` 與請求 UUID，讓 HTML 502 與應用 JSON 錯誤可區分。這只能縮小故障層，不能回推 2026-09-29 Edge 個案的確切根因。

## 尚待業務決策

正式訂單欄位與來源、實際出貨的意義、待出貨計算、客戶/品項識別、幣別、部分出貨、實體倉庫收發與盤點尚未核定。本增量沒有推測單檔原型的「訂單減出貨」為物理庫存，也不把發票等同出貨。

## 升級與回復

ASUS 使用 `deploy/upgrade-asus-management.py`。它核對原 runtime commit、待處理工作、archive SHA、依賴與 unit 未變，先在新 release 執行測試；停止服務後保存原始 SQLite/WAL/PDF 和一致性 SQLite snapshot，再切換。切換後檢查管理首頁及匿名 API 401。**一旦產生管理文件或草稿，不可把指標盲目退回不懂用途隔離的舊 runtime**；失敗時先停止新程式、等待退出，再查詢是否有管理寫入。已有管理寫入便保留新 `current`、新 DB/PDF 與備份，服務維持停止且輸出 `HARD STOP`，需人工修復版本前進；不啟動舊版，也不還原舊 snapshot 抹除新資料。沒有管理寫入時仍使用同一份 live DB/PDF 回切，保留切換後新增的診斷 session、文件與工作。以合成資料實測：舊 `eb2f928` runtime 在新 schema 但尚無管理寫入時可讀原本 1 份診斷文件；新增 1 份管理文件後，它會把 2 份都當成診斷文件顯示。因此回切閘門是必要的，並非僅為保守假設。正式資料備份、磁碟與服務資源仍按 ASUS 現有作業約束管理。

工程測試使用合成 PDF 與可控 Gemini adapter，不構成真實 Google API、公司 Edge、真實訂單品質或人工驗收證據。
