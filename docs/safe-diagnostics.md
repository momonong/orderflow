# 試用引導與安全診斷候選

本文件描述本機候選，尚未部署至 ASUS。管理頁給使用者的路徑為「選科雅採購憑單 PDF → 在瀏覽器讀取品項 → 人工核對 → 確認並儲存」。讀取前後的狀態會明示尚未儲存；只有按下確認儲存才將 PDF 和人工記錄送至網站主機。本機讀取不呼叫 Google。另一條「AI 自動分析」路徑會在使用者另行確認後將 PDF 送交 Google，可能使用額度。

## 證據語義

- 瀏覽器每次操作產生 UUID trace_id，每次 API 請求產生 UUID request_id。它們只是查找標籤，不作身分或授權憑證。
- 伺服器 journal 的 http.received 只證明請求進入應用程式；db_committed 只在儲存方法返回後寫入；response_written 只證明應用程式寫入 socket 返回，不證明瀏覽器收到。response_write_unknown 表示寫入時連線已斷，但先前提交仍可能成功。
- real_job.ai_start 表示應用程式開始呼叫 Google；沒有 Google HTTP 回應時，不推斷 Google 是否收到或計費。ai_response 表示 adapter 收到並讀到回應；format_pass、db_committed 分別表示格式檢查與工作結果保存完成。
- 瀏覽器記錄 send、http_received、marker_checked、json_parsed、job_rendered 等觀察。job_rendered 只在成功顯示結果後記錄；結果顯示失敗時記 render_failed，不能同時把該次結果當作顯示成功。client_observation 是未受信任的瀏覽器回報，不能代替伺服器記錄。HTTP 502 且缺少應用程式標記時，只能歸為應用程式外的未知失敗；沒有應用程式事件可能是入口阻擋、日誌容量上限、服務故障或網路中斷，不能單憑缺席定位某一層。
- Google HTTP 狀態與固定錯誤類別依實際回應記錄；DNS、TLS、拒絕連線、逾時未知、HTTP 截斷或格式異常只在對應的 Python 例外可辨時分類。逾時不分辨連線與讀取階段，也不推斷 Google 端處理結果。

## 限制與保護

- 只向 systemd journal 輸出固定欄位：版本、UTC 毫秒時間、事件、階段、固定路由、狀態、耗時、UUID、固定錯誤或上游分類。格式驗證失敗另可記固定原因、從 0 起算的品項索引、白名單欄位、固定型態及超長文字的區間（201–500、501–1000、超過 1000 字），不記原始值。沒有 PDF 內容、品項、檔名、key、Cookie、IP、User-Agent、header、query、HTTP body、Google 回應正文或原始例外文字。
- POST /orderflow/api/diagnostics 必須登入、同源 Origin、既有自訂 header；每筆只接受固定欄位與列舉、最多 12 個事件和 4096 bytes。每登入會話最多每分鐘 12 次，伺服器整體最多每分鐘 600 個 audit 事件；超過上限不影響業務請求。瀏覽器只在已看到應用程式標記時嘗試自動送出最少事件，5 秒節流，失敗後停止自動送出，且不重試業務操作。複製資訊仍可手動使用。
- 不新增監控服務、持久化診斷資料表、全域 journald／Caddy／Cloudflare 設定。journal 的實際保存期限取決於主機現有設定；本候選未改它。容量滿或紀錄寫入失敗時，不能把缺少事件解讀為請求未發生。
- 操作者依使用者複製的 trace_id 查詢：journalctl -u orderflow --since 'YYYY-MM-DD HH:MM:SS' --grep 'TRACE_UUID' --no-pager。依 request_id 或 job_id 再查同次請求與背景工作。搜尋輸出仍須在授權的本機終端處理，不貼出整段 journal。

合成示例：瀏覽器看到 management_jobs / http_received / 502 / HTML / MISSING，同一 trace_id 沒有應用程式 received。結論為「瀏覽器收到非應用程式的 HTML 502，後端是否接到此請求尚不確定」；不是「Google 503」或「資料庫失敗」。若同一 job_id 有 real_job.db_committed，但建立工作請求只有 response_write_unknown，應先查工作列表與記錄，禁止自動重送付費請求。

## 本機驗收矩陣

使用合成資料與受控 stub，需核對：正常保存；請求未達應用程式；非應用程式 HTML 502；Google HTTP 503；逾時未知；資料已提交但回應斷線；工作 GET 的 JSON／渲染失敗；重新載入取回已保存資料且不重送 AI；診斷端點失敗與剪貼簿拒絕；惡意欄位不得進 journal；登入會話、文件用途、冪等鍵與本機解析「確認前不傳 PDF」回歸。Chrome 桌面、390px 與鍵盤流程須驗證；Edge 可用時再驗。真實 Google、客戶 PDF、ASUS 部署與人工接受不在此階段。

## 此候選已核對的範圍

- 2026-10-02 本機 Python 65 項、JavaScript 10 個測試入口通過。HTTP 與 Google 分類使用受控 stub；503 的背景工作維持 unknown，並保留上游 HTTP 503。斷線寫入、HTML 502、JSON 解析與渲染失敗分別有回歸。
- 在隔離的 127.0.0.1:18769、本機暫存資料庫及 Codex 內建瀏覽器，合成科雅 PDF 於畫面讀到 2 筆，狀態明示尚未儲存。按確認之前，伺服器 documents、jobs、management_local_sources 都是 0。鍵盤 Enter 可展開格式細節；390px 視窗下整頁沒有水平溢出；複製的診斷資訊有追蹤識別，沒有合成品項、客戶文字或檔名。暫存測試服務已停止。
- 此瀏覽器驗證是 Codex 內建瀏覽器；獨立 Chrome、Edge 與實際公司瀏覽器仍未驗。正式站與 ASUS 服務未改動；沒有呼叫真實 Google，也沒有上傳真實 PDF。資料庫持久化及重新載入回歸由受控測試與前一版合成瀏覽器證據支持，此候選尚未重做完整手動保存流程。

## 渲染事件修正後的獨立預覽

- 修正提交 `7116c3e` 將管理頁及診斷測試頁的 job_rendered 移到結果成功顯示後；提交 `179014b` 保證清理畫面也持續失敗時，render_failed 與白話狀態仍可見。10 個 JavaScript 測試入口通過，包含持續 DOM 故障；本次只改瀏覽器端程式和測試，Python 65 項沿用前述同一後端版本的結果。
- 預覽網址為 `http://127.0.0.1:18770/orderflow/`，systemd user unit `orderflow-safe-diagnostics-preview-7116c3e.service` 僅綁 `127.0.0.1`，資料目錄 `/tmp/orderflow-safe-diagnostics-preview-7116c3e/data`。合成測試密碼可在本機互動終端讀 `/tmp/orderflow-safe-diagnostics-preview-7116c3e/test-password`；不要貼進聊天。此預覽與原有 8765 服務使用不同程序、連接埠及資料目錄。
- 以 Codex 內建瀏覽器重新核對：合成科雅 PDF 在畫面讀到 2 筆，尚未按確認前資料庫 documents、jobs、management_local_sources 均為 0；390px 視窗無整頁水平溢出，鍵盤 Enter 可展開格式說明。預覽的 app.js、manage.js、diagnostics.js 回應位元與本機候選一致；登入頁 200、匿名 bootstrap 401。可用瀏覽器清單沒有獨立 Chrome 或 Edge，因此兩者尚未驗證。
- 預覽完成後停止命令：`systemctl --user stop orderflow-safe-diagnostics-preview-7116c3e.service`。停止不會刪除隔離資料或原有 8765 服務；清理該暫存資料目錄前應先確認不再需要驗收證據。

## 公司端一次試用後的查找方式（本機候選，尚未上線）

表姐在公司用已授權的正式入口試一次，記下當地時間及畫面狀態。若已有失敗／未知的辨識工作，先選取該筆，再按管理頁「複製診斷資訊」；同一份摘要會附上該筆 job ID 與固定錯誤欄位，無需再複製第二次。若請求一開始就失敗，直接按複製按鈕即可。只交給被授權處理 OrderFlow 的人，不附 PDF、API key 或網站密碼。剪貼簿被拒時，頁面會選取可手動複製的摘要。若整個網站頁面都無法載入，瀏覽器程式無法提供複製按鈕；此時只記瀏覽器錯誤畫面和時間，主機是否收到請求仍是未知。

後台操作者在 ASUS 的授權終端，先確認查詢時間採用該主機本地時區，將時間窗限制在試用前後數分鐘。以下 `TRACE_UUID`、`REQUEST_UUID`、`JOB_UUID` 是收到的識別字，不是憑證；不要把整段 journal 貼到聊天或工單。

```bash
sudo journalctl -u orderflow.service --since 'YYYY-MM-DD HH:MM:SS' --until 'YYYY-MM-DD HH:MM:SS' --no-pager -o cat --grep 'TRACE_UUID'
sudo journalctl -u orderflow.service --since 'YYYY-MM-DD HH:MM:SS' --until 'YYYY-MM-DD HH:MM:SS' --no-pager -o cat --grep 'REQUEST_UUID'
sudo journalctl -u orderflow.service --since 'YYYY-MM-DD HH:MM:SS' --until 'YYYY-MM-DD HH:MM:SS' --no-pager -o cat --grep 'JOB_UUID'
```

先以 trace 找 `http.received` 與 `http.db_committed` 內的 request/job ID，再依 job ID 找背景 `real_job`。`http.db_committed` 的 `job_id` 連接建立工作的請求與背景工作；`real_job.db_committed` 在失敗時只代表**失敗狀態已保存**。`response_written` 只證明應用程式寫入 socket 返回；若是 `response_write_unknown`，先查工作列表，切勿自動重送可能計費的 AI 工作。`ai_start` 後無 `ai_response`，不能推定 Google 是否收到或計費。`result_format_invalid` 的固定原因可定位型態、欄位或長度區間；一般程式異常應是 `internal_unknown`，不能冒稱 Google 格式錯。

合成測試驗證的事件順序範例（識別字省略，非公司端實際日誌）：`http.received → http.db_committed(job_id) → http.response_written(202)`，同一 job 為 `real_job.start → ai_start → ai_response → db_committed → result_format_invalid(code=RESULT_FORMAT_INVALID, format_reason=FIELD_TYPE, item_index=0, field=qty, actual_type=number)`。這能證明應用程式收到並保存了失敗工作，且驗證器拒絕第 1 筆 `qty` 的型態；不揭露該值、PDF 內容，也不代表上游無計費。另一個合成測試讓 adapter 拋出非格式 `ValueError`，記為 `internal_unknown`，不誤記 `result_format_invalid`。

查無事件時，先核對 journal 權限、時間窗、保留期限、該次服務程序是否曾重啟及每分鐘 600 筆的 audit 限流；瀏覽器上傳診斷本身也可能被入口攔截、登入失效、5 秒節流或每會話每分鐘 12 次限制。缺席不能證明請求未到主機。此候選的詳細格式原因尚未部署，ASUS 現行版只能依既有事件與工作狀態判讀；上線需另行批准與驗證，不應把本機合成測試當成表姐的公司端驗收。

格式原因修正提交 `db1264a` 與一鍵複製補強 `c6a5995` 已通過本機 Python 67 項、JavaScript 10 個測試入口。新後端只在測試程序中執行；上節的 18770 預覽是在此修正前啟動的 Python 程序，尚未載入新後端程式，不可用它驗收 `format_reason`。原有 8765 預覽及 ASUS 正式服務也未因本次提交而重啟或切換。

唯讀 journal 鏈路另用 18770 隔離 user unit 驗證：帶合成 trace/request UUID 的匿名 `GET /orderflow/api/bootstrap` 回 401，`journalctl --user -u orderflow-safe-diagnostics-preview-7116c3e.service --grep <trace UUID>` 查到同一 request 的 `http.received → http.response_written(401)`。這只證明本機 user unit 的日誌可按識別搜尋；正式 ASUS system unit 的 journal 權限、保留與上線後新格式原因，仍須由有權限者另行核對。
