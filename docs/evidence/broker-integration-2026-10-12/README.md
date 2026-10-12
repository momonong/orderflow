# ASUS OrderFlow → Broker 合成驗收（2026-10-12）

## 已完成

- 依 main 原文批准的整合部署與合成測試範圍執行；沒有推送、合併、啟用付費、修改 ingress 或無關服務。
- OrderFlow 部署來源：`316ec314b7878994c3c50eed562f3be9f990dfbd`；Broker 固定來源：`81725056364b9c88296a38c93a745e90000fa57e`。
- [封包 seal](deployment-seal.json) 固定 source/archive/config/readiness/runner hashes；ASUS 核對封包與 Python 3.14.4 編譯 54 個 Python 檔案通過。
- [受保護執行結果](deployment-result.json)：`deployment=passed`、`acceptance_complete=true`、舊列與檔案保留 gate 通過。
- 停寫 migration 時精確核對全部舊 rows/schema/user_version 與舊 PDF；重開服務後核對舊 owner／業務 identities 及舊 PDF hash，允許正常資料更新與登出刪除 session_auth。
- 舊原始資料與停止時 SQLite snapshot 保留於 ASUS `/var/backups/orderflow/before-broker-316ec314b787/`；沒有還原舊 DB 或刪除資料。
- [部署後獨立健康核對](post-deploy-health.json)：current 為上述 OF 來源，PID 476403、active/running、NRestarts 0、NeedDaemonReload=no；Broker PID 473639 未變。使用既有 Host 與 18081 路徑，頁面 200／匿名 health API 401。

## 唯一合成工作

| 項目 | 證據 |
| --- | --- |
| job | `8a522759-9705-44fe-a506-4146097d4a68` |
| document | `17a4f154-7fa1-4cac-9904-33100955dafe` |
| request key | `of-smoke-60e2f4f4dff043d5a5fae809f4378fbe` |
| PDF SHA-256 | `7970e48bb5b75e7b7d8d8c3ebbd892729fa35f83eb0dab2886e2029afec198af` |
| task SHA-256 | `ac3b1ecec8dd2dc7897b4ee9a1ebb28d7f4bd95daa180905640a859c89f079bf` |
| job／phase／gateway | `done`／`complete`／`completed` |
| finish／truncated／usage_unknown | `stop`／`false`／`false` |
| expected items | `true` |
| output 上限 | 唯一 job snapshot `2048`；正常服務配置 `8192` |

使用無私人資料的 941-byte PDF，經 Store/make_task 建立 durable job，再由正式 OrderFlow worker → Broker → Google 免費目標完成。預期內容為合成發票 OF-SYNTHETIC-ONE、SYNTHETIC CUSTOMER、2026-10-12、USD、SYNTHETIC WIDGET／SYN-001、數量 2、單價 3.50、金額 7.00；逐欄比對通過。保留同一 job/key；沒有未知工作換 key、直接 provider fallback 或第二份合成工作。

[Broker 同 key 唯讀稽核](broker-same-key-audit.json) 已核對原檔 SHA-256 `89309070f6966889d88e007ac2dbca9bb1024082a39df879d74c31b23db0aa26`，並驗證外層 queue key 與內層 execution key 對應：

- queue attempt_count／budget_used／max_attempts 均 1；CountTokens receipt 1、generation attempt 1，均 HTTP 200。
- CountTokens 回報 681，保守輸入預估 1074；生成於 06:38:31.200 UTC 派送，在 preflight 到期 06:40:30.646 UTC 之前。
- Google 實際回報 input 661／output 134 tokens；`usage_source=provider_reported`、`ledger_basis=settled_provider_usage`、`ledger_state=completed`。
- CountTokens 計數、保守預估與生成回報是不同量測，不互相覆寫或當作官方剩餘配額。
- 此次稽核在 06:40:55.733 UTC 只執行兩次 GET、provider POST 0，沒有新 job/key 或生成重試。
- 原 Broker 證據位於其 repo 的 `docs/evidence/pdf-integration-2026-10-12/of-smoke-60e2f4f4dff043d5a5fae809f4378fbe.audit.json`，由 Broker task 提交於 `3ff8a8d`。本目錄保存完全相同的非秘密原文。

協調者 `01a0e6ca-81be-7292-a917-2a346e64d043` 另轉交 main 的公開頁唯讀核對：`https://momonong.me/orderflow/` 與首頁各回應 200。該核對只證明公開頁可達；本 task 未重做外網測試，也未將它視為登入、瀏覽器、公司網路或人工驗收。

## 首次停止與具體修正

第一次來源 `468285a2815cdab6e97a6ba1855d038ec7276d24` 在 readiness credential mode gate 停止。原 claim、root packet 與 started marker 均保留；preflight、raw backup、release 均未建立，provider POST 0、舊 PID/current 不變。未重跑原封包。

[metadata-only 診斷](credential-metadata.json) 證明 systemd 提供 root:root 0440、父0550且服务可開啟。修正只允許受信任的 systemd credential mount 0440；其他 group-readable 檔案、可寫或符號連結父目錄仍拒絕。protocol 16 項、readiness 15 項通過；[ASUS 唯讀 recheck](credential-recheck.json) 通過後，才以新的 316ec31 來源／封包進行部署。

## 驗收範圍與限制

- 此次證明指定合成 PDF 在正式 worker/Broker/Google 路徑可完成；不代表任意 PDF 品質、官方剩餘配額或整體負載容量。
- 沒有驗收 HTTP 上傳／表單登入／瀏覽器視覺／公司網路／人工接受，不稱為 HTTP E2E。
- 既有 8 MiB 隔離假 transport 壓力驗證與這次 941-byte 真供應商合成驗收是不同證據。
- Broker local caps 不是官方剩餘用量；沒有啟用付費。正常服務的 input-bound evidence 到期仍應 fail closed。
- 工程驗收通過，不代表使用者已接受畫面或辨識品質。
