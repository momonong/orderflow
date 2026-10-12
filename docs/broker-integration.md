# OrderFlow Broker 接入與部署候選

狀態：使用者已批准 OrderFlow → API Quota Broker 整合部署與測試，含必要的 8 MiB PDF／用量預估修正、兩服務配置與重啟、保留舊資料，以及一份無私人資料 synthetic PDF 的 Google 免費 API 測試。此候選尚未部署、未執行新 live、未取得使用者驗收；不合併或推送、不啟用付費。批准不等於資源、admission、備份或驗收閘門已通過。

人類授權來源為 OrderFlow main turn `01a12e8a-a24f-7302-b1dd-093da432975e`，原文：「批准這次 OrderFlow → API Quota Broker 整合部署與測試。請沿既有任務完成必要的 8 MiB PDF／用量預估修正、兩服務配置與重啟，保留全部舊資料，再用一份無私人資料的合成 PDF 測試 Google 免費 API；不用再問例行批准，不合併推送、不啟用付費。」本文件修正只在本機進行；主 task 準備釘選來源、資源 gate 與執行窗口後才進入已批准的操作。

## 來源與版本

OrderFlow 原接入基準是 `971aadf340d7ff85b9822e21ab76b9726318ae5c`，已完成本機候選 `6c13431c97fcfb7b95a7a341c617855f4ecc5380`。本輪 8 MiB 配置與部署 gate 調整位於同一 `codex/broker-integration`；部署前須釘選包含這些調整的新 source commit、封存 SHA-256、配置與 unit，不能以原候選 SHA 冒充新來源。現行 OrderFlow `12cbed79dc81cf2b266fbddd9cbf6e806d7ff80a` 由本 task 先前唯讀核對 ASUS current/systemd/HTTP：service active/running、NRestarts=0、頁面 200、匿名 API 401；本輪唯讀回報 MemoryCurrent 約 35.7 MB／MemoryMax 768 MiB，ASUS 記憶體約 2 GiB、MemAvailable 1459 MiB、可用磁碟約 88 GB。sudo -n 不可用，root-only encrypted credential 無法由非特權檢查 stat；credential 可供給性仍需有權限者透過正式 gate 核對。這些檢查沒有遠端變更或 Broker/provider 呼叫；空閒記憶體不代表已預留容量。歷史部署及資料保留回條見[登出修復部署紀錄](logout-retention-deployment-2026-10-12.md)。Broker 接入候選尚未部署。

正式 Broker 接入依據固定 commit `2145d9ba7b47c153af2022102439d67a1c789365` 的 [README](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/README.md)、[client-integration.md](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/docs/client-integration.md) 與 [provider-capability-expansion.md](https://github.com/momonong/api-quota-broker/blob/2145d9ba7b47c153af2022102439d67a1c789365/docs/provider-capability-expansion.md)。它們定義 Gateway JSON、typed media、queue、結果及不重播語義。

本輪 Broker 固定來源為本機未發布 commit `81725056364b9c88296a38c93a745e90000fa57e`，完整 `docs/orderflow-pdf-integration.md` SHA-256 為 `58b14d9c17e6f3f63eab3e70b68de8bc6b3468564ca8a8a2638d149a6eea3d34`。2026-10-12 已獨立唯讀 SSH 核對 ASUS current `release-d7fccdccbd40e6b9c3d25746bf46b2b1ff1b605b3d37f13d7beec7409a655afa`、unit SHA `cba27e1e58c4ff2c63b2b0d76c45e291c8815f1c51967217972342f202bf8ab8`、active/running、PID 473639、NRestarts 0、NeedDaemonReload=no、無 drop-in、384/512 MiB MemoryHigh/Max、swap0、CPU50%、Tasks32、NOFILE128。Broker root 受保護 config/payload/active pins 及 GET credential 仍須由本次 root readiness gate 獨立核對。

Broker ready 收據為其 repo `docs/evidence/pdf-integration-2026-10-12/release-8172505/ready.json`，SHA `fbf4c879f419412198d5716823281db08d7d46777f4da6e31040efa8843d5e1a`；observed 05:55:13Z、部署窗口 expires 07:55:13Z。receipt 只是具時效的比對來源，不能取代 root gate 的現場重驗。早期 `3f5e0aca` 僅保留歷史，不作此次部署依據。

## 配置與 admission 閘門

後端以明確 `--recognition-mode direct|broker` 選路徑，預設 direct。每個持久化工作固定 mode、instance、credential binding、provider/model 及 payload；未知結果不切換至 direct 或其他供應商。

[JSON 候選](../deploy/orderflow-broker.example.json) 包含完整 `BrokerConfig.snapshot()` 欄位，沒有 credential 值。`token_file: null` 留給後端 CLI 的 `--broker-token-file` 覆寫。候選 endpoint 僅允許 literal `http://127.0.0.1:<port>`；正式候選為同主機、同 network namespace 的 `18084`，不推定跨機器或容器 localhost 可用。

| 項目 | 此候選 | 邊界 |
| --- | --- | --- |
| OrderFlow 上傳 | 8 MiB | 原上傳上限保留；保存 PDF 不等於可送 Broker |
| Broker decoded PDF | 8 MiB | 本階段已批准；部署前須與 Broker 配置及資源 gate 一致 |
| Broker HTTP request / result | 16 MiB / 4 MiB | 檢查實際序列化 bytes，含 base64、prompt、schema |
| 正式業務 output bound | 8192 | 可配置且不超過已知 target cap；model/target admission 仍重驗 |
| 本階段唯一 synthetic smoke output bound | ≤2048 | 已授權的一份合成 PDF 工作；不取代業務預設 |
| provider / model | google / gemini-3.5-flash-lite | Broker 已啟用 features；本輪真 PDF 尚待驗收 |
| max_attempts / wait_policy | 1 / wait | 應用 submit 只有一次；queue 自行等待 admission |
| HTTP timeout | 12 秒 | 單次有界；逾時查原工作，不自動重送 |

Broker 已套用 8／16／4 MiB，OrderFlow 本輪配置尚未部署。ASUS 原生隔離 8 MiB 測試已通過：42.821 秒、cgroup peak 237,977,600 bytes、swap0、CountTokens/generation fixture 各一次、真 provider0；PDF parser 5次皆原限制內，OrderFlow PID不變。client/sampler在獨立cgroup；cgroup峰值不包含所有client啟動頁面，不能當部署併發負載保證。已批准的範圍可直接完成必要修正與驗證，不重問例行批准；未通過閘門時保持新派送關閉。

PDF input token bound 與用量預估由 Broker 負責；OrderFlow 不從檔案大小、頁數、base64 或文字公式猜填，不增加 caller resource bound 欄位，也不另呼叫 CountTokens。本輪部署方案由 Broker 在持久化 queue 的 generation dispatch 前負責 CountTokens preflight；固定政策為 count totalTokens 加 20% 並加256、效期120秒，以HMAC綁定完整wire/model；count intent與請求charge先持久化，count未知/失敗不generation、不重播。count+generation須有兩個POST的本地cap空間；結果仍標estimated，count未知charge不退款。不能把 CountTokens actual count 直接宣稱嚴格上界，或以它冒充最終 provider billed usage。

範例配置保持安全預設，維持 `input_bound_verified=false`、evidence/expiry=null，新送出 fail closed。正式 packet 僅在 shared readiness 通過後使用 evidence `broker-pdf-81725056364b9c88296a38c93a745e90000fa57e`；配置 expiry 不超過當前 Google account/capacity 2026-10-22T03:53:20.337024+00:00，部署/合成動作另受兩小時 receipt 窗口限制。`ready=true` 僅表示本機配置與 protected credential 可讀，不證明 Broker features、帳戶、免費資格、配額或實際 admission。

`6c13431` 的 BrokerConfig strict snapshot 欄位與本輪 JSON 相容；8／16／4 MiB、8192 在其現有安全上界內。變更只影響新工作的配置，不改寫舊 task/hash/config snapshot。舊工作仍以原 key/binding 查詢；若改變 credential runtime 路徑或 endpoint，既有 mapping 可能以 CONFIG_CHANGED 停止調解，必須保留並核對，不能改 binding 繞過。

## Credential 與 service 候選

[systemd drop-in 候選](../deploy/orderflow-broker.service.conf.example) 保留既有 `orderflow.service` 的 `User/Group=orderflow`、login-auth、0700 StateDirectory、hardening、restart 與 MemoryMax/TasksMax；完整 ExecStart 延用原 host/port/data-dir/public-origin，再加入明確 Broker mode、JSON 路徑與 credential 路徑。

候選使用 `LoadCredentialEncrypted=client_token:/etc/api-quota-broker/credentials/client_token.cred`，必須保留 `client_token` 名稱綁定。只有經批准的主機操作者能核對其可解密性、root-only 加密來源與 per-service runtime 權限。後端從 `%d/client_token` 讀取 regular file，拒絕 symlink 與 group/other permissions；支援 0400/0600 credential。CLI 只帶路徑，不帶值；值不進前端、JSON、Git、argv、repr 或日誌。

專用部署 gate 需將無秘密值的 `/etc/orderflow/broker.json` 設為 `root:orderflow 0640`，並將目前 `root:root 0700` 的 `/etc/orderflow` 調整為 `root:orderflow 0710`，僅增加 app group 的 traverse 權，讓服務可讀明確檔名；不增加列目錄權、不改 login-auth 的原權限或秘密值。這是已批准接入所需的設定變更，須在部署回條記錄，不能把它描述為完全不寫系統設定。

不得改 owner/mode 讓 OrderFlow 讀取一般使用者的 `/run/api-quota-broker-client/client_token`。provider key、Doppler runtime、admin token、queue key 留在 Broker。client token 是可信合作式客戶端的共享權限，沒有專案／使用者 ACL；request key 前綴不能提供租戶隔離。這個服務候選未執行 decrypt、copy、chmod、chown、安裝 drop-in 或 restart。

## 工作持久化與結果

首次 POST 前，同一 SQLite transaction 先保存 job/document/owner、外層 Broker request key、完整 task、canonical payload SHA-256、prompt/schema/payload versions、完整非秘密 config snapshot 與執行 mode。送出 claim 先 commit；claim 與實際 POST 之間若 crash，可能留下尚未送出的 unknown，仍不能重播。endpoint/credential binding 與 payload hash 不符時停止原工作調解。

Broker Task 為 `capability=text_generation`、google、固定 model；typed user messages 放既有完整 prompt 與 inline PDF base64 document，`options.json_schema` 僅將 type 值遞迴轉為小寫，requirements 為 `document_input`、`structured_output`。原 prompt、欄位、required、空 items 及 direct 語意沿用。

提交至 `POST /v1/queue` 後以原外層 key 查 `GET /v1/queue/{key}`／`result`，不驅動 queue tick。HTTP timeout、unknown、遺失提交回條及程序重啟都不 retry POST、不跟 redirect、不用 proxy、不另起 key/model/instance，也不 fallback。GET 依持久化退避、next_retry_at 與 expires_at 有限等待；結果預設自提交起 TTL 86,400 秒。過期沒有內容不代表可重新執行。

本輪唯一 POST 重試例外是 Broker 候選的明確零接受回應：HTTP 503、完整 JSON content type、精確物件 `{"error":"work_busy"}`、`Connection: close`，以及單一數字 Content-Length 等於實際讀至 EOF 的 bytes，且沒有 Transfer-Encoding。Broker 在讀 body 前拒絕，未保存 queue 或派送；其他 503、HTML、缺 marker／framing header、額外欄位或尾端 bytes、重複 JSON key、截斷與 timeout 都不能證明未接受。即使大 body 傳送被 BrokenPipe 中斷，也只能從同一 socket 的完整精確回應取得此證據，不能另送探測請求。

確定 busy 後才保存 `busy_N`，同一 job/key/payload 按 1/2/4/8 秒、8 秒封頂有限退避；一輪最多 240 秒或原工作 TTL 較短者。每次 POST 前先提交 `submitting_N`；該 claim 後 crash 仍只 GET。耗盡轉 `busy_paused`（已知未接受），使用者可確認「繼續原辨識工作」，以 owner/purpose 驗證的 `POST .../jobs/{id}/resume`、空物件續原 mapping，不生成新 key 或延長原 TTL。未知、已接受、完成、失敗、到期均不能使用此入口。GET result busy 保留原接受狀態與 metadata，只延後 GET，不能取得 POST 重試權限。正式啟用須通過固定 Broker 現場 gate。

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

專用候選為 [upgrade-asus-broker.py](../deploy/upgrade-asus-broker.py)，隔離保資料／停止條件測試見 [test_upgrade_asus_broker.py](../tests/test_upgrade_asus_broker.py)。共用 `deploy/broker_readiness.py` 驗固定receipt、actual current/manifest/payload/unit/config/effective properties與protected credential GET diagnostics/catalog。最終執行由固定 `run-broker-integration.py` 與 `transport-broker-integration.py` packet 控制；來源/封包SHA另存本機及遠端非秘密seal。未封版或驗證失敗不能執行。

預設 preflight 也會寫入 root-only staging、備份副本與 receipt，並以 PrivateNetwork 的 transient systemd service 驗證 per-service client credential；不讀出 token、不派送 provider，不切 current、不改 live DB/config 或正式 OrderFlow service。它是有寫入的隔離驗證，**preflight PASS 不等於部署**。前次 preflight 的 venv/cache 不重用；每次從核對 archive 與可信舊 runtime 建立獨立 `.broker-preflight-<commit>-<pid>`，`--deploy` 才重新建立正式 release。已存在正式 release（含 symlink）直接拒絕，避免未核對 runtime 被 root 執行。

本 gate 在 stop 前檢查備份空間、原 base unit 與無額外 drop-in、無既有 Broker mapping，以及 archive/runtime ownership／permissions（包含 symlink target 與 parents）。正式 `--deploy` 才停止 writer、重做停寫副本與 raw backup、遷移、設定 0640 JSON／0710 parent／唯一 drop-in、切 current、daemon-reload/restart 與靜態/API 後檢。以下為已批准範圍內的保資料流程；本文件修正不執行 root script：

1. 釘選新 OrderFlow source SHA、source archive SHA-256、Broker 正式 capability release SHA、舊 runtime、JSON／drop-in 位元。核對實際 current/unit/依賴、所有 writer、待處理工作、Broker ledger/queue 及同機資源。原 code-only gate 的「unit 不變」條件此次須明確改為審查 base unit 加唯一候選 drop-in，不能悄悄略過。
2. 停寫前用 SQLite backup API 建預檢 snapshot，配對私有 PDF 副本；在隔離副本上初始化候選 Store。核對 integrity/foreign_key_check、每一舊表所有列與 schema、user_version、各 PDF SHA；只允許新增 `broker_jobs`（含主鍵／外鍵與既定欄位），不能改舊資料。online PDF/DB 若因寫入無法形成一致副本，停止並取得一致備份再試跑。
3. 固定來源／資源 gate 通過、執行窗口確認後才停止 OrderFlow writer，要求 `orderflow` 帳號無殘留程序且待處理工作符合停止條件。建立唯一 root-only 0700 backup：raw SQLite/WAL/SHM、全部 PDF/私有檔案及獨立一致 snapshot；逐檔 SHA 與 snapshot/現場全部舊表逐列比對。將 unit/drop-in/config 與來源 SHA receipt 一起保留，不保存秘密值。
4. 對停寫 snapshot 再試跑 migration，才在 live state 新增 `broker_jobs`，再次核對全部舊表、關係及 PDF。正式 source、依賴與安全設定吻合後才切 current 並啟動；bound=false 的驗證保持不派送。後檢分別核對來源／靜態 routes、匿名 API 401、登入後 owner/mode、credential 路徑、worker 調解、bound 拒送及原資料保留。
5. 停寫後任一步失敗為 **HARD STOP**：停止受影響 writer，保留 live DB/PDF、原 backup、Broker ledger/queue、原 request/execution mapping 與收據供核對。不停止其他服務、不清 unknown hold、不刪 queue/ledger、不得換 key重跑。

一旦可能有出站、Broker 接收工作或任一新寫入，不可用舊 snapshot 覆寫或盲啟舊 runtime。即使沒有已知出站，也必須先證明全體 writer quiescent、無任何新寫入、original mapping/ledger/queue 一致且舊 runtime 支援保留資料，才另行審查回復；優先保留資料修復前進。root-only 備份存在不是還原演練證據。

## 已批准範圍、剩餘閘門與最小 live 驗收

部署與測試授權已生效；剩餘工作是釘選 Broker CountTokens／可信 bound 與本地 cap 政策、capability source/features、8 MiB ASUS 資源 gate、credential 供給、完整 migration／備份 gate，以及受控執行窗口。原資料、帳本、queue/mapping 與未知 hold 全部保留，不啟用付費，不合併推送。遇到超出本階段必要修正或新增資料／工作時，才回 main 決策。

最小 live 候選只用一份無私人資料、可人工核對的 synthetic PDF，先保存其 hash、完整 prompt/schema、預期 items 及一個持久化業務 job；provider/model 固定 google/gemini-3.5-flash-lite，`max_attempts=1`、output ≤2048。先以固定配置/policy、catalog、diagnostics核對指定target、features、本地cap、expiry與worker；diagnostics是input/output各1的最小admission，ready_targets/state不能證明本份PDF通過。完整wire的CountTokens及admission由正式Broker worker執行。在已批准的唯一合成工作與固定執行窗口內 submit 一次，後續僅 GET 原工作／結果；不可因失敗或未知結果另建第二份 synthetic 工作。

CountTokens 是 Broker 負責的額外認證外部 POST，須在其持久化 queue dispatch 前保存獨立 preflight intent/result 與原工作關聯；OrderFlow 沒有第二條 CountTokens 呼叫路徑。固定 source/policy 必須明列 margin、完整 wire/model 綁定、效期、失敗停止、呼叫上限與執行窗口，才可在本階段必要用量預估修正中執行；實作固定於8172505；本輪真provider結果仍待唯一job驗證。

本階段的受控準備器為 [orderflow-broker-smoke.py](../deploy/orderflow-broker-smoke.py)：固定單頁英文 Invoice PDF **941 bytes**，SHA-256 **`7970e48bb5b75e7b7d8d8c3ebbd892729fa35f83eb0dab2886e2029afec198af`**，明示一列 SYNTHETIC WIDGET／SYN-001、qty=2、unitPrice=3.50、amount=7.00、USD，只有非私人合成資料。這份小 PDF 不證明 8 MiB 的 live 品質；8 MiB 的 wire／記憶體／parser 資源 gate 另做離線／ASUS native 驗證。

helper 先以 root-only intent 固定 owner/document/upload/job/外層 Broker keys，再以 `orderflow` 有效 uid/gid 寫 DB/WAL/PDF，finally 恢復 root 才寫 private receipt。專用 owner 沒有 session_auth 或可登入 token；random token hash 只留 DB，不進 intent／receipt／輸出。原文件路徑以固定 UUID 原子寫入，Store 的 job+mapping transaction 保留；任何重入都恢復同一工作，已存在 job 只讀不初始化 Store，failed/expired/busy 也不新增工作。正式 config 保持 8192，該 job 的 immutable snapshot 才降為 2048。

receipt 僅輸出 opaque IDs、來源／PDF／task hashes 與安全 metadata，包含 max_output_tokens=2048（請求上限，不是 actual usage）、gateway_state、finish_reason、response_truncated、usage_unknown。job done 時另回 expected_items_match boolean，比對單列 PDF 明示的 invoiceNo/client/product/code/currency/date/unit 與 decimal qty/unitPrice/amount；不輸出 result 原文。prepared/failed 的 match 為 null，未知／completed_usage_unknown 用量保持未知，不用 match=true 冒充已結算或使用者接受。

CLI 必填 reviewed readiness receipt／SHA，共用readiness重新驗固定8172505與現場policy/free/worker；pure fixtures只注入離線fake gate。缺失、漂移、過期或不符均拒新的prepared job。helper 不送submit/provider/CountTokens；共用readiness只GET diagnostics/catalog，也不開始、停止或恢復正式 worker。root wrapper 停寫、有效 Broker/free/worker gate、唯一合成工作派送與 GET 驗收仍由主 task 在固定窗口統一執行；本文件不增加窗口或第二份工作。

驗收分別記錄：完整 metadata、JSON/schema、synthetic 預期 items、草稿人工確認、provider actual usage／未知 hold，以及 timeout/restart 不重播。合成成功不代表私人文件、所有大小／頁數、多品項品質或使用者接受。任何 unknown 保留原工作與收據，停止新的派送。

## 本機驗證紀錄

主 task 完成整合 gate：完整 Python **129 項通過，53.862 秒**，本機完整輸出保留於 `/tmp/orderflow-broker-full-python.log`；JavaScript **11 個 cjs + 4 個 mjs，共 15 個入口通過**，包含新增 Broker UI 與依實際 bootstrap 流程修正的 startup fixture。

完整 gate 後另補「已完成 Broker → 明確 direct 新工作」API fixture；[broker_jobs 測試](../tests/test_broker_jobs.py) **18 項通過，3.884 秒**，輸出位於 `/tmp/orderflow-broker-jobs-final.log`。範圍涵蓋完整舊表 row／PDF SHA 保留的 additive migration、人工確認與 logout、新 attempt 原子性、mode inactive，以及原 key 調解邊界。最後的 UI 文案與 direct-after-complete 調整另通過 8 個相關 JavaScript 入口及兩份 JS 語法檢查；主流程使用一般中文，未知用量只保留在可複製診斷資訊與診斷測試頁報告。協定與 UI 的 fixture 驗證仍不代表真實服務、瀏覽器視覺或 provider 驗收。

本輪 8／16／4 MiB、8192 JSON 候選已離線經 BrokerConfig 復建並核對所有欄位，預設 ready=false／INPUT_BOUND_REQUIRED；恰好 8 MiB 的合法合成 PDF 經既有 make_task 序列化為 11,185,655 bytes，低於 16 MiB，保留 max_attempts=1／output=8192。這只是本機配置／payload 檢查，沒有 HTTP 或 provider 呼叫。drop-in 延用原 ExecStart 全部參數、保留 client_token credential ID，並用最終 app 的 argparse 宣告解析完整候選命令（包含 `--broker-token-file`）通過。解析過程沒有啟動 app 或讀取 credential。`git diff --check` 通過。

fixture、JSON 解析及 unit 靜態核對均不表示部署、服務重啟、可信 bound 已配置或 live-ready。本機候選 commit 不等於部署版本；尚無此候選的部署／新 live 回條或使用者接受證據。

### 本輪部署前補驗（2026-10-12，本機）

- JSON 的 8/16/4 MiB、8192 設定通過 strict snapshot roundtrip；完整 8 MiB PDF Task 約 11.19 MB，在 16 MiB HTTP 上限內，`max_attempts=1`。配置仍 bound=false，沒有派送。
- busy 協定／工作與 API **13 + 28 = 41 項通過，12.341 秒**，記錄在 `/tmp/orderflow-broker-busy-focused.log`。涵蓋雙工作長於 7 秒 busy、原 key 重啟調解、240 秒截止與明確續作、owner/purpose/body 隔離、GET busy 保留接受狀態，以及真實 loopback 8 MiB 大 body 提早拒收／BrokenPipe／損壞回應。最後 transport deadline 取原 TTL 與 busy window 較短者另有兩邊界定向驗證。
- 隨後收緊 busy framing，**13 項協定重驗通過，6.718 秒**（`/tmp/orderflow-broker-busy-framing.log`）；缺 Connection close 且 CL 隱藏尾端 extra bytes 的補充案例也通過。這些是同一測試集的補驗，不另累計成不同功能數量。
- 管理／診斷頁 busy 暫停與原工作續作、取消確認、遺失續作回條只 GET、不生成新 key 的 UI fixture 通過；7 個相關既有 JS 入口及兩份 syntax check 通過。
- 部署 gate **9 項隔離測試通過**，記錄在 `/tmp/orderflow-broker-deployment-gate.log`：完整舊 row/PDF、意外 schema、資料損壞、symlink、已有 mapping、runtime 漂移與停寫後 HARD STOP。獨立審查曾發現既有 staging venv 未完整驗證的復用風險，已移除復用並補拒絕測試；舊可信 runtime 及 symlink target/parents 仍須 root-owned、不可 group/other write。另有前檢後 nested symlink 被 root 跟隨複製的重現，已改為複製時保留 symlink，並在 trial chown／migration 前拒絕，補驗外部合成檔未被讀入副本。

ASUS 目前只讀核對：舊 runtime `12cbed79...`、無 drop-in、active/running、NRestarts=0、頁面 200、匿名 API 401；可用磁碟約 88 GB、RAM 約 1459 MiB。root credential 路徑及正式 DB 尚無權獨立檢視，`sudo -n` 不可用；上述 fixture 不是實際舊資料或 ASUS 新版 cgroup 驗收。Broker effective unit/config、execution policy、target/free eligibility 與 worker 的固定證據未交付前，專用腳本 CLI 明確停用 `--deploy`，不能以 current symlink 取代 readiness。

本輪合成準備器 [定向 fixture](../tests/test_broker_smoke.py) **17 項通過，1.770 秒**；涵蓋六個持久化 crash gaps、並行重入唯一 owner/PDF/doc/job/key、full config/owner/purpose/scenario/hash 篡改拒絕、receipt 修補、uid/gid 恢復、busy 安全投影、expected items match/mismatch 與未知用量分離。py_compile 與 diff check 通過；沒有讀真 credential、HTTP、provider 或正式狀態寫入。專用 root gate 的 8 項隔離 tests 由主 task 核對，仍不代表遠端執行或目前 packet 可用。

## 封包及執行保護

`run-broker-integration.py` 執行保資料部署後，再停 OrderFlow writer，以私有 durable intent 建唯一合成 job，正式 worker start 後僅查同job最多300秒。unknown/failed/timeout保留原job，不能再執行部署指令或換key。停寫migration時全部舊列/schema必須精確相同；重開後查舊業務/owner身份仍存在與舊私有檔案/PDF hash，允許合法登入更新和logout刪session_auth，不將可用期間合法更新誤當migration損壞；異常停止受影響writer、保留backup。root packet `started.json`與local transport claim均為exclusive/fsync，不自動重播。

私有 sudo 路徑只重用固定 Broker `bootstrap_transport_v2.py` SHA `4527a42c01b4d7c05300a75d8b54f1b0574ebe994214e1ed20f50e749f393d3a` 的短命guard/精確PAM/READY/私管道元件。Doppler入口僅記 `asus-maintenance-bootstrap/dev` secret名稱 `ASUS_MORRIS_SUDO_PASSWORD`；值不進argv/env/file/log。OF使用獨立operation/rootSHA/claim/backup/unit，絕不重跑Broker once。兩端swap0/core0/non-dump；取得固定ASUS/morris精確提示後才在私管道取值；正式審查拒絕即停止，不換路徑。root先核所有上傳檔案hash/regular/owner/mode，再複製root700封包並執行。

這次最低live驗收已由main收斂為Store/make_task→正式worker→Broker→Google；正常8192、唯一job2048。原HTTP上傳、表單登入、瀏覽器視覺、公司網路及人工確認仍未驗，不稱HTTP E2E。

本輪完整Python回歸168項59.999秒通過，新增runner保留/停写/不重播4項通過；readiness13項與transport4項定向測試通過；整合後Broker相關79項14.185秒通過；此前migration9項0.531秒通過，敏感資料格式掃描20檔0命中。

### 首次封包停止與憑證相容性修正

本機來源 `468285a2815cdab6e97a6ba1855d038ec7276d24` 的首次受保護部署，在 readiness observer 的 credential mode 檢查停止。原 local claim、root packet 與 `started.json` 保留，不重跑該操作。獨立唯讀診斷確認 preflight/raw backup/release 均未建立、沒有合成工作或 provider POST，舊 current `12cbed79...`、PID 460208 維持運行。

metadata-only 診斷確認 systemd materialized credential 為 root:root `0440`，父目錄 `0550`，OrderFlow UID 999/GID 983 可開啟；未輸出憑證內容。原 `mode & 0077` 拒絕此正常交付模式。readiness 與正式 client 的修正限定 `CREDENTIALS_DIRECTORY` 對應 `/run/credentials/<unit>/client_token`、root 所有且不可 group/world write 的真目錄，以及 root:root `0440` 檔案／`0500` 或 `0550` 直接父目錄；其他 group-readable token 仍拒絕。原私有 `0400`/`0600` 支援、NOFOLLOW、大小及內容格式檢查保留。修正後須以新來源、新封包及新操作識別繼續，不清除首次失敗證據。

修正後 protocol 16 項 6.752 秒、readiness 15 項 0.021 秒通過。ASUS 獨立唯讀 recheck 以 readiness source SHA `82da6f17076f01d17ba6557bba6710ec4680d9438d390ac2e53a7b5ba12df8db` 執行完整固定 receipt／host／credential GET gate，結果 passed；仍為 deployment retries 0、provider POST 0、首次 preflight/raw backup/release 均不存在。非秘密 receipt 留於首次本機封包 `credential-recheck-r4.result.json`。這項結果只證明修正後 readiness，不是新部署或 Google 合成驗收。
