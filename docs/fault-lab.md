# 本機連線故障實驗

這個工具以**真實 OrderFlow 後端和診斷頁**走登入、上傳、建立辨識工作及查詢結果的 HTTP 路徑。實驗程序另啟固定的 loopback 代理與假 Google HTTP 服務；只在該程序內把既有 Gemini adapter 的固定 endpoint 指到假服務。登入密碼、bcrypt hash 與假 AI key 每次啟動時在記憶體產生，不會寫入報告、終端或 Git。PDF 是空白合成頁；SQLite 位於獨立暫存目錄，程序結束時移除。

## 一次跑完並閱讀報告

從本 repo 根目錄執行：

```bash
uv sync --locked
uv run --locked python -m tools.fault_lab run-all --output /tmp/orderflow-fault-reports
```

每個場景會產生 `report.json` 和 `report.md`，根目錄有 `summary.json`。若只需單一場景：

```bash
uv run --locked python -m tools.fault_lab run --scenario receipt_lost_after_commit --output /tmp/orderflow-receipt-report
```

JSON 分開記錄 `injection_truth`、`script_observations`、`backend_audit_output`、`final_db_state`、查詢恢復與判定。人類可讀報告也分別列出注入位置與次數、客戶端症狀、正式 audit 輸出的 request/trace/job、最終資料庫狀態與未知。`backend_audit_output` 是原診斷器經白名單及節流後，實際寫到此實驗程序 stderr 的事件。`response_written` 僅表示後端寫入自身 socket，不證明瀏覽器已收到。腳本的 `PASS` 不是瀏覽器或公司網路驗收。

## 瀏覽器操作

每次僅啟動一個場景；在 repo 根目錄執行：

```bash
uv run --locked python -m tools.fault_lab serve --scenario receipt_lost_after_commit
```

終端會列出 `http://127.0.0.1:<隨機埠>/__lab/session` 與合成 PDF 的絕對路徑。用本機瀏覽器直接開該網址，實驗器會透過**正式登入與設定 key API**建立獨立合成 session，再導向 `/orderflow/test/`。頁首標記說明 Google 路徑在實驗程序內指向本機假服務。按「檢查連線」、選終端列出的合成 PDF、確認上傳，再按真正辨識按鈕；該按鈕仍走應用的真實工作建立和 Gemini adapter，卻不會連到 Google。若 key 在 15 分鐘後失效，重新直接開 `/__lab/session` 可保留同一瀏覽器 session 的文件並更新假 key。以 `Ctrl+C` 停止，三個 loopback listener 和暫存 SQLite/PDF 隨程序關閉。切換場景需重新啟動；沒有網路可操作的故障開關。

瀏覽器驗收需分別記錄頁面載入、合成上傳、建立工作時的畫面、重新整理後的同一 job 與結果，以及報告複製或手動選取是否可用。若瀏覽器工具的點擊或選檔未送達，明確標為 UI 未驗證，不能以 API 腳本代替。

## 場景與可觀察邊界

| 場景 | 注入位置與預期觀察 |
|---|---|
| `normal` | 無故障；一筆 job、一個假上游呼叫，結果可查回。 |
| `browser_google_blocked` | 只讓本機瀏覽器探針回 403，後端仍能呼叫假上游；這是行為模擬，不是企業 DNS/TLS 測量。 |
| `post_html_403`、`post_html_502`、`post_html_200` | 代理在辨識 POST 前回 HTML；上傳及頁面路徑仍可用，後端沒有建立工作。 |
| `post_missing_marker` | 辨識 POST 回正常 JSON，但拿掉應用識別 header。 |
| `post_delay`、`post_disconnect`、`post_truncate` | 分別讓回應跨過前端 5 秒逾時、在送往後端前斷線、或截斷後端回應。 |
| `receipt_lost_after_commit` | 後端建立 job 並回 202，代理在送給客戶端前斷線；bootstrap 和 GET 查回同一筆已保存結果，不自動重送 AI。 |
| `result_lost_after_commit` | 工作已完成，第一次結果 GET 的回應遺失；第二次 GET 查回同一筆結果。 |
| `diagnostics_blocked` | 辨識與查詢可用，診斷上傳 POST 被代理回 403 HTML；瀏覽器仍應能複製或手動選取本機報告。 |
| `ai_401`、`ai_403`、`ai_429`、`ai_503`、`ai_timeout`、`ai_invalid` | 假上游實際透過 HTTP 回錯誤、超過 25 秒逾時或回無效結果；分辨 failed 與 unknown。 |

本工具不修改正式站、HP／ASUS、Caddy、Tunnel、DNS、防火牆或憑證，也不連真實 Google。實驗只能驗證各種可觀察故障與安全恢復路徑，**不能據此斷定公司端的根因**。TLS MITM、真實企業政策、真實 Google 額度與內容品質，以及人工接受，都不在本機模擬證據內。
