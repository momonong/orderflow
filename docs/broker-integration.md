# OrderFlow Broker 接入與部署候選

狀態：本機接入候選，尚未部署、未執行新 live、未取得使用者驗收。此文件提供可審查的配置與遷移流程；不是 root 執行腳本，也不授權服務、帳戶、資源或網路變更。

## 來源與版本

OrderFlow 本階段來源基準是 `971aadf340d7ff85b9822e21ab76b9726318ae5c`；接入程式及本文件以 `codex/broker-integration` 的同一個本機候選提交交付，精確 commit 見交付回條。部署前必須釘選經批准的候選 commit、封存 SHA-256、候選配置與 unit。現行 OrderFlow `12cbed79dc81cf2b266fbddd9cbf6e806d7ff80a` 由本 task 先前唯讀核對 ASUS current/systemd/HTTP：service active、頁面 200、匿名 API 401；該核對沒有遠端變更或 Broker 呼叫，之後收束為本機接入階段。歷史部署及資料保留回條見[登出修復部署紀錄](logout-retention-deployment-2026-10-12.md)。Broker 接入候選尚未部署。

正式 Broker 接入依據固定 commit `2145d9ba7b47c153af2022102439d67a1c789365` 的 [README](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/README.md)、[client-integration.md](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/docs/client-integration.md) 與 [provider-capability-expansion.md](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/docs/provider-capability-expansion.md)。它們定義 Gateway JSON、typed media、queue、結果及不重播語義。

Broker repo 的 `docs/orderflow-pdf-integration.md` 另以本機未發布候選 commit `3f5e0aca8a33639147f38e31a6a09f80a71e4e53` 引用；該檔 SHA-256 已核對為 `571a73bd4165761a639bf88405071d704df22c60abdb85cc27fd47cdf2f3395d`。其 Google 3.5 features、可信 input bound、8 MiB 資源與 credential 提案尚未發布或部署；不能把該未發布候選冒充正式契約或現場 live-ready 證據。後續採用前須重新核對候選內容及正式發布 SHA。

## 配置與 admission 閘門

後端以明確 `--recognition-mode direct|broker` 選路徑，預設 direct。每個持久化工作固定 mode、instance、credential binding、provider/model 及 payload；未知結果不切換至 direct 或其他供應商。

[JSON 候選](../deploy/orderflow-broker.example.json) 包含完整 `BrokerConfig.snapshot()` 欄位，沒有 credential 值。`token_file: null` 留給後端 CLI 的 `--broker-token-file` 覆寫。候選 endpoint 僅允許 literal `http://127.0.0.1:<port>`；正式候選為同主機、同 network namespace 的 `18084`，不推定跨機器或容器 localhost 可用。

| 項目 | 此候選 | 邊界 |
| --- | --- | --- |
| OrderFlow 上傳 | 8 MiB | 原上傳上限保留；保存 PDF 不等於可送 Broker |
| Broker decoded PDF | 1 MiB | 延用 Broker 候選記錄的現場限制；送出前明示限制 |
| Broker HTTP request / result | 2 MiB / 4 MiB | 檢查實際序列化 bytes，含 base64、prompt、schema |
| 正式業務 output bound | 8192 | 可配置且不超過已知 target cap；model/target admission 仍重驗 |
| 後續單次 smoke output bound | ≤2048 | 僅有限 live 候選，不取代業務預設 |
| provider / model | google / gemini-3.5-flash-lite | Google 3.5 PDF + structured features 尚未 live enable／驗收 |
| max_attempts / wait_policy | 1 / wait | 應用 submit 只有一次；queue 自行等待 admission |
| HTTP timeout | 12 秒 | 單次有界；逾時查原工作，不自動重送 |

應用 media 可配置至 8 MiB、HTTP request 至 16 MiB、result 至 32 MiB；8 MiB 的部署選擇目前沒有 ASUS cap／記憶體 gate 授權。完整替代 8 MiB 流程需另核對 Broker 大 body 入站與 worker 重疊、cgroup peak、OrderFlow 共存工作及 PDF parser CPU/wall bounds；provider concurrency=1 不限制 HTTP body buffer。不能僅提高兩個 flags 就視為資源驗收完成。若暫用 1 MiB，UI 需在辨識前顯示缺口，main 決定是否接受此階段範圍。

PDF 的 input token bound 由 Broker 管理者在 target 配置；應用不從檔案大小、頁數、base64 或字串 token 公式猜填，也不增加 caller resource bound 欄位。正式 bound 目前缺失，因此例子為 `input_bound_verified=false`、evidence/expiry=null，應用新送出保持 fail closed。之後須同時有管理者確認、非秘密 opaque evidence ref 與未過期且帶時區的 expiry，才開啟應用配置閘門。`ready=true` 僅表示本機配置與 protected credential 可讀，不證明 Broker features、帳戶、免費資格、配額或實際 admission。

## Credential 與 service 候選

[systemd drop-in 候選](../deploy/orderflow-broker.service.conf.example) 保留既有 `orderflow.service` 的 `User/Group=orderflow`、login-auth、0700 StateDirectory、hardening、restart 與 MemoryMax/TasksMax；完整 ExecStart 延用原 host/port/data-dir/public-origin，再加入明確 Broker mode、JSON 路徑與 credential 路徑。

候選使用 `LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred`，必須保留 `client_token` 名稱綁定。只有經批准的主機操作者能核對其可解密性、root-only 加密來源與 per-service runtime 權限。後端從 `%d/client_token` 讀取 regular file，拒絕 symlink 與 group/other permissions；支援 0400/0600 credential。CLI 只帶路徑，不帶值；值不進前端、JSON、Git、argv、repr 或日誌。

不得改 owner/mode 讓 OrderFlow 讀取一般使用者的 `/run/api-quota-broker-client/client_token`。provider key、Doppler runtime、admin token、queue key 留在 Broker。client token 是可信合作式客戶端的共享權限，沒有專案／使用者 ACL；request key 前綴不能提供租戶隔離。這個服務候選未執行 decrypt、copy、chmod、chown、安裝 drop-in 或 restart。

## 工作持久化與結果

首次 POST 前，同一 SQLite transaction 先保存 job/document/owner、外層 Broker request key、完整 task、canonical payload SHA-256、prompt/schema/payload versions、完整非秘密 config snapshot 與執行 mode。送出 claim 先 commit；claim 與實際 POST 之間若 crash，可能留下尚未送出的 unknown，仍不能重播。endpoint/credential binding 與 payload hash 不符時停止原工作調解。

Broker Task 為 `capability=text_generation`、google、固定 model；typed user messages 放既有完整 prompt 與 inline PDF base64 document，`options.json_schema` 僅將 type 值遞迴轉為小寫，requirements 為 `document_input`、`structured_output`。原 prompt、欄位、required、空 items 及 direct 語意沿用。

提交至 `POST /v1/queue` 後只以原外層 key 查 `GET /v1/queue/{key}`／`result`，不驅動 queue tick。HTTP timeout、unknown、遺失提交回條及程序重啟都不 retry POST、不跟 redirect、不用 proxy、不另起 key/model/instance，也不 fallback。GET 依持久化退避、next_retry_at 與 expires_at 有限等待；結果預設自提交起 TTL 86,400 秒。過期沒有內容不代表可重新執行。

| 外部情況 | 應用處理 |
| --- | --- |
| queued / waiting / running、503 pending | 保存原 key，有限等待與查詢 |
| unknown / timeout / 404、malformed metadata | 保留 mapping 與未知狀態；404 不證明未執行 |
| 409 conflict、binding/hash 改變 | 停止並保留原工作供核對 |
| failed / rejected / cancelled / expired | 不從名稱推定未執行；沒有 safe non-execution 證據保持文件鎖 |
| queue completed | 再核對 gateway_state、execution_key、result 與完整性 |

同一文件有未解決 Broker 工作時，換瀏覽器 key 或重按辨識會取得原工作，不建立第二個送出。只有先前 Broker 工作已完整完成、且使用者明確確認再次辨識並指定 `new_attempt=true`，才可建立新的業務辨識；新工作使用當前明確配置的 Broker 或 direct 路徑，原工作 mode 保持不變；公開模式仍受每文件最多 10 次工作限制。失敗／過期沒有 safe unexecuted 證據時保持鎖，須另經維運核對及授權處理。

result 是完整 Gateway outcome；其內層 `request_key` 必須等於 metadata.execution_key，不能覆寫外層業務 key。只接受 outcome.state 與 metadata.gateway_state 一致的 completed/completed_usage_unknown；文字來自 `result.text`。要求 `finish_reason=stop`、`response_truncated` exact false；max_tokens/length、missing/null/unclassified 均拒收為不完整。JSON parse 之後仍使用原業務 validator；結果與完成 metadata 同一 transaction 保存。

`completed_usage_unknown` 的答案與用量分開：可有完整答案，但 provider usage 仍未知，不補送以求結算。管理草稿照原流程人工核對、修正、確認後才成正式記錄；工程完成不等於人工接受。資料保留範圍包含原 SQLite、PDF、session、draft、annotations、record 與 Broker mapping；task payload/PDF 本身是內容，持續留在既有私有資料目錄與受控備份中，不能出現在錯誤摘要。

## 遷移、備份及回復候選

現有 [migration_checks.py](../deploy/migration_checks.py) 可核對 SQLite integrity、session/document/job 關係與 PDF 大小／SHA；[shared-trial helper](../deploy/upgrade-asus-shared-trial.py) 示範完整舊 schema/表列 Counter 比對、foreign_key_check、backup API、regular tree、quiescent 與 HARD STOP。這些 helper 的既有升級腳本釘選不同舊 runtime／schema／unit，不能直接重跑本次 Broker 接入。

需另審查的部署 gate 流程如下，本階段不提供或執行 root script：

1. 釘選新 OrderFlow source SHA、source archive SHA-256、Broker 正式 capability release SHA、舊 runtime、JSON／drop-in 位元。核對實際 current/unit/依賴、所有 writer、待處理工作、Broker ledger/queue 及同機資源。原 code-only gate 的「unit 不變」條件此次須明確改為審查 base unit 加唯一候選 drop-in，不能悄悄略過。
2. 停寫前用 SQLite backup API 建預檢 snapshot，配對私有 PDF 副本；在隔離副本上初始化候選 Store。核對 integrity/foreign_key_check、每一舊表所有列與 schema、user_version、各 PDF SHA；只允許新增 `broker_jobs`（含主鍵／外鍵與既定欄位），不能改舊資料。online PDF/DB 若因寫入無法形成一致副本，停止並取得一致備份再試跑。
3. 經部署批准後才停止 OrderFlow writer，要求 `orderflow` 帳號無殘留程序且待處理工作符合停止條件。建立唯一 root-only 0700 backup：raw SQLite/WAL/SHM、全部 PDF/私有檔案及獨立一致 snapshot；逐檔 SHA 與 snapshot/現場全部舊表逐列比對。將 unit/drop-in/config 與來源 SHA receipt 一起保留，不保存秘密值。
4. 對停寫 snapshot 再試跑 migration，才在 live state 新增 `broker_jobs`，再次核對全部舊表、關係及 PDF。正式 source、依賴與安全設定吻合後才切 current 並啟動；bound=false 的驗證保持不派送。後檢分別核對來源／靜態 routes、匿名 API 401、登入後 owner/mode、credential 路徑、worker 調解、bound 拒送及原資料保留。
5. 停寫後任一步失敗為 **HARD STOP**：停止受影響 writer，保留 live DB/PDF、原 backup、Broker ledger/queue、原 request/execution mapping 與收據供核對。不停止其他服務、不清 unknown hold、不刪 queue/ledger、不得換 key重跑。

一旦可能有出站、Broker 接收工作或任一新寫入，不可用舊 snapshot 覆寫或盲啟舊 runtime。即使沒有已知出站，也必須先證明全體 writer quiescent、無任何新寫入、original mapping/ledger/queue 一致且舊 runtime 支援保留資料，才另行審查回復；優先保留資料修復前進。root-only 備份存在不是還原演練證據。

## 待批准與最小 live 驗收

仍需 main 確認：可信 PDF input bound／本地 cap 政策、Broker capability 發布與 features 啟用、1 MiB 階段範圍或 8 MiB ASUS 資源 gate、credential 供給與合作式權限界線、新固定 release/migration gate、兩服務的個別部署與維運範圍，以及有限 live 額度／資料授權。此文件沒有批准其中任一步。

最小 live 候選只用一份無私人資料、可人工核對的 synthetic PDF，先保存其 hash、完整 prompt/schema、預期 items 及一個持久化業務 job；provider/model 固定 google/gemini-3.5-flash-lite，`max_attempts=1`、output ≤2048。先用同完整 payload explain 核對 target、features、可信 bound、cap、expiry 與 worker；explain 不派送 provider，但其通過也不能取代送出時 admission。取得相應部署與 live 批准後才 submit 一次，後續僅 GET 原工作／結果。

CountTokens 是額外的認證外部 POST，尚未授權或實作，不能藏在一次 job 的預算中。若選此方案，需另定 margin、完整 wire/model 綁定、期限、持久化 preflight、失敗停止與成本／呼叫上限並取得批准。

驗收分別記錄：完整 metadata、JSON/schema、synthetic 預期 items、草稿人工確認、provider actual usage／未知 hold，以及 timeout/restart 不重播。合成成功不代表私人文件、所有大小／頁數、多品項品質或使用者接受。任何 unknown 保留原工作與收據，停止新的派送。

## 本機驗證紀錄

主 task 完成整合 gate：完整 Python **129 項通過，53.862 秒**，本機完整輸出保留於 `/tmp/orderflow-broker-full-python.log`；JavaScript **11 個 cjs + 4 個 mjs，共 15 個入口通過**，包含新增 Broker UI 與依實際 bootstrap 流程修正的 startup fixture。

完整 gate 後另補「已完成 Broker → 明確 direct 新工作」API fixture；[broker_jobs 測試](../tests/test_broker_jobs.py) **18 項通過，3.884 秒**，輸出位於 `/tmp/orderflow-broker-jobs-final.log`。範圍涵蓋完整舊表 row／PDF SHA 保留的 additive migration、人工確認與 logout、新 attempt 原子性、mode inactive，以及原 key 調解邊界。最後的 UI 文案與 direct-after-complete 調整另通過 8 個相關 JavaScript 入口及兩份 JS 語法檢查；主流程使用一般中文，未知用量只保留在可複製診斷資訊與診斷測試頁報告。協定與 UI 的 fixture 驗證仍不代表真實服務、瀏覽器視覺或 provider 驗收。

JSON 候選已離線經 BrokerConfig 復建並核對所有欄位，預設 ready=false／INPUT_BOUND_REQUIRED；drop-in 延用原 ExecStart 全部參數、保留 client_token credential ID，並用最終 app 的 argparse 宣告解析完整候選命令（包含 `--broker-token-file`）通過。解析過程沒有啟動 app 或讀取 credential。`git diff --check` 通過。

fixture、JSON 解析及 unit 靜態核對均不表示部署、服務重啟、可信 bound 已配置或 live-ready。本機候選 commit 不等於部署版本；尚無此候選的部署／新 live 回條或使用者接受證據。
