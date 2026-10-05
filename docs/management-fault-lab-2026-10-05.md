# 管理流程本機故障模擬（2026-10-05）

## 結論與邊界

八個案例均符合現有管理 API 契約；這輪沒有找到應用缺陷，沒有改動正式應用程式碼。實驗使用隔離的 SQLite、合成空白 PDF、本機 HTTP 代理與本機假 Google，真正呼叫 `GeminiAdapter` 的 HTTP 路徑，並擷取正式 `orderflow_audit` 輸出。假 Google 回應是固定合成欄位，**不證明真實文件辨識品質**。本次沒有呼叫真實 Google、ASUS、HP 或公司資料，也沒有推送、合併、部署或進行公司端人工驗收。

重現：

```bash
.venv/bin/python -m tools.management_fault_lab --output /tmp/orderflow-management-fault-lab-recheck
```

原診斷實驗器的 18 個場景另外重跑通過：

```bash
PYTHONPATH=. .venv/bin/python tools/fault_lab.py run-all --output /tmp/orderflow-fault-lab-recheck
```

本輪逐案例原始紀錄位於 `/tmp/orderflow-management-fault-lab-20261005-run-id/`，每案含唯一 `run_id`、應用版本 `0.3.0`、診斷 build `diag-20261002-01`、HTTP 觀察、代理注入事件、正式 audit 事件、假 AI 次數與 DB 結果；`summary.json` 的 `run_id` 與各案報告一致且八個值互不相同。原診斷回歸紀錄位於 `/tmp/orderflow-fault-lab-regression-20261005-final/`。這些是本機暫存紀錄，重跑時指定新目錄即可保留舊報告。

## 案例與判定

| 組別 | 假設與注入 | 可觀察症狀與持久結果 | 分類 |
| --- | --- | --- | --- |
| 1：正常 PO | 不注入故障；合成採購單 | POST 202、GET `done`；假 AI 1 次、job 1 筆；用人工確認 API 建立 rev 1，重送相同確認維持 rev 1 | 契約通過 |
| 1：發票回條遺失 | 管理 job POST 後端回 202 後、代理關閉客戶端連線 | 客戶端沒有 POST 狀態；bootstrap 找到原 job，GET `done`；假 AI 1 次、job 1 筆；發票確認 rev 1 可重讀 | 預期的恢復 |
| 1：PO 結果 GET 遺失 | 等 job `done` 且結果已保存，再讓第一個 GET 的後端 200 回條遺失 | 客戶端第一個 GET 無狀態；下一個 GET 讀到同一 `done` 結果；假 AI 1 次、job 1 筆，確認 rev 1 可重讀 | 預期的恢復 |
| 2：同鍵併發與範圍 | 假 AI 暫停於 HTTP 入口，兩個客戶端平行重送同一 session／document／request_key | 觀察到 `queued → running → done`；平行重送均 200 且指向原 job，假 AI 僅 1 次。管理 API 對同鍵不同 `scenario` 回 400 `BAD_JOB_REQUEST`；同鍵跨 document 可另建 job（202），符合 `(session_id, document_id, request_key)` 範圍 | 契約通過 |
| 2：上游結果未知 | 假 Google 對管理工作回 HTTP 503 | job 為 `unknown`／`AI_HTTP_UNKNOWN`；同鍵重送回 200 同一未知 job，沒有再呼叫 AI | 正確保留未知 |
| 2：本機工作槽繁忙 | 第一個工作暫停在假 AI，另一個 request key 新建同文件工作 | 第二筆工作 `failed`／`AI_RATE_LIMITED`；job `steps` 與 audit 均沒有 `upstream_http_status`；假 AI 只收到第一筆 | 契約通過 |
| 2：假上游 429 | 假 Google 對管理工作回 HTTP 429 | job 同樣為 `failed`／`AI_RATE_LIMITED`，但 `steps` 與 audit 都記錄 `upstream_http_status=429`；假 AI 收到 1 次 | 契約通過 |
| 3：金鑰過期 | 先完成正常工作，再只在隔離程序把金鑰 TTL 加速到 0.1 秒，透過真實 HTTP 重設短效金鑰並等它過期 | 既有 `done` 結果 GET 200；新 request key 的 POST 回 409 `KEY_REQUIRED`；job 與 AI 呼叫數維持 1 | 契約通過 |

代理的「已寫入 socket」與後端的 `response_written` 不能證明瀏覽器收到回條；因此回條遺失案例分開記錄代理注入真相與客戶端觀察。正式 audit 在 job 建立、AI 開始／完成、DB 提交與讀取時都有相應事件。`unknown` 不自動重送，避免無法判定上游是否已處理時重複送出。

管理流程的 PO 與 invoice 都需要人工確認後才存成 record set。確認的 invoice row 只是發票資料，**不是出貨或已交貨證據**。本輪沒有模擬公司真實帳號、真實文件、真實 Google、瀏覽器完整點擊或公司網路故障，因此不能據此做正式辨識或公司端驗收。

## 觀察到的實驗器問題

第一次重跑原診斷 `run-all` 時，新增的三個管理專用情境也被舊診斷流程列入，造成三個套件選擇失敗；原有 18 個診斷案例當時全數通過。根因是共用 `SCENARIOS` 同時供兩個 CLI 使用。已讓舊 CLI 僅列舉診斷情境；重新執行後 **18/18 通過、exit 0**。這是實驗器的選擇錯誤，沒有顯示應用缺陷。
