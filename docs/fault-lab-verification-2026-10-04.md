# 本機故障實驗驗證，2026-10-04

**結論：**在本機合成環境，18 個 HTTP 故障場景皆符合注入症狀與資料庫預期；真實診斷頁在「建立工作回條遺失」後顯示結果不明，重新整理後查回同一筆已保存結果。這些證據不能判定公司網路根因或真實 Google 辨識品質。

- 起點：`b245b1a1da252f08a2f39a192346892d44832dc0`（當時 `origin/main`）；工作分支：`codex/fault-lab`。只新增本機實驗工具、測試與文件；未推送、合併或部署。
- `PYTHONPATH=. .venv/bin/python tools/fault_lab.py run-all --output /tmp/orderflow-fault-lab-suite-20261004-final`：最終版 18/18 `scenario_expectation_met=true`。逐場景 JSON/Markdown 位於上述目錄；其中 [回條遺失報告](/tmp/orderflow-fault-lab-suite-20261004-final/receipt_lost_after_commit/report.md)列出代理注入、客戶端斷線、後端 `received`／`db_committed`／`response_written`、假上游一次呼叫、同一 job 的 GET 恢復。該目錄是本機暫存證據，不是永久發行物。
- `.venv/bin/python -m unittest discover -s tests -q`：88 tests 通過，包含新增的真正 HTTP 回條遺失／查詢遺失與代理 Host、Origin、CSRF 邊界。最後增補假上游跨站拒絕檢查後，`tests.test_fault_lab` 4 tests 再次通過。`git diff --check` 通過。
- 內建瀏覽器實際點擊：連線檢查通過，431 位元組合成 PDF 上傳成功；回條中斷時畫面提示不要立即重做，報告記錄 `NETWORK_ERROR`；重新整理後畫面顯示工作 `c00f6835-8c1b-45ba-9784-a2384b0388ee` 與合成品項。唯讀 SQLite 查到 1 文件、1 工作、`done`、結果已保存。詳見 [瀏覽器觀察 JSON](fault-lab-browser-observations-2026-10-04.json)。
- 診斷 POST 受阻場景：瀏覽器仍可檢查基本連線；按複製後頁面顯示成功，手動展開可見 2294 字、含追蹤識別的報告。隨後無憑證探針回 401，顯示一次性代理故障已先被消耗；依當時只有瀏覽器活動推定由該頁觸發。未獨立讀取到系統剪貼簿內容，因此僅將按鈕狀態及手動備援列為已觀察。

各場景使用真正 OrderFlow 應用、Gemini adapter、loopback 假上游與固定代理；沒有真實 Google 呼叫、真實憑證、正式資料庫或遠端服務變更。`browser_google_blocked` 僅是本機行為模擬，不代表企業 DNS/TLS/代理政策。工作建立或查詢結果不明時，恢復路徑是查詢既有工作；沒有自動重送 AI。`backend_audit_output` 擷取正式診斷器經白名單和節流後輸出的事件，`response_written` 仍不是瀏覽器收到的證明。
