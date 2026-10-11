# 登出資料保留修復：ASUS 部署後核對

2026-10-12（Asia/Taipei）已完成部署與唯讀後檢。這證明指定修復版本已提供服務、匿名存取仍被拒絕；不等於手機 Safari 登入後人工驗收或原四筆 Invoice 已恢復。

## Git 與部署版本

- [PR #16](https://github.com/momonong/orderflow/pull/16) 已將固定 head `12cbed79dc81cf2b266fbddd9cbf6e806d7ff80a` 合併至 main，merge commit 為 `9177287284f5d80eb79dac9d42a3972bbfb47ffb`。
- 封存 head 與 merge 的 Git tree 同為 `0c52b4b3d34454480c00ef57628c527c6f95713b`。ASUS 使用固定 head 封存部署。
- 使用者在 ASUS 終端執行固定 SHA runner，回條為三項 SHA `OK`、`ASUS logout retention upgrade: PASS`，以及 `Existing SQLite rows and files preserved; authenticated browser trial still required`。
- 本次 root-only 備份為 `/var/backups/orderflow/before-logout-retention-12cbed79dc81`。資料保留與備份依據為使用者執行的 root gate 回條；後檢沒有讀取、還原或獨立檢查該備份內容，也沒有查閱正式 Invoice 資料。

| 部署輸入 | SHA-256 |
| --- | --- |
| 固定 head `source.tar` | `67063467669aa3b064d303a63818ebb8e7decdeb50053ed611b151f61bfdcdb1` |
| `upgrade.py` | `2922c7f292806b1d42736d96b6723fc3730ebcfe42a7d5069b62e3c2b4366934` |
| `run-on-asus.sh` | `ef86af446a362f04ddc62d632e7d64990b5cddb5de7fc37778183407647be363` |

閘門沿用原保資料升級程序，釘選舊 runtime `b85eedc5ed403e2e084f7dbcbc34d67244f55304`、新 runtime `12cbed79...`；要求依賴、service unit 不變與 SQLite schema 完全相同。停寫前完成候選測試、online snapshot 與副本試跑；停寫後核對完整資料表與原檔備份，才切換。失敗採 HARD STOP，不盲目覆寫正式資料。此次已成功執行，舊 runtime 條件不再成立，不能重跑一次性指令。

## ASUS 唯讀後檢

2026-10-12 05:05 起核對，主機為 `asus-ubuntu2604-server`：

- `/opt/orderflow/current` 指向 `/opt/orderflow/releases/12cbed79dc81cf2b266fbddd9cbf6e806d7ff80a`。
- `orderflow.service`：active/running、enabled、Result=success、NRestarts=0。啟動時間為 2026-10-12 05:02:22（Asia/Taipei；systemd 原輸出為前一日 21:02:22 UTC）。
- OrderFlow listener 為 `127.0.0.1:18081`；資料目錄權限 `0700 orderflow:orderflow`。
- release 的 `orderflow/app.py` SHA 與固定 head 相同。
- 依 `deploy/upgrade-asus-shared-trial.py` 的完整 19 個 `STATIC_ROUTES`，逐一核對 release 檔案及 loopback HTTP 回應，全部 200 且與固定 head 位元相同，包含管理、診斷、整合、手冊與 PDF.js/worker。
- loopback 的 `api/bootstrap`、`api/management/bootstrap`、`api/integration/bootstrap`、`api/shared-documents`、`api/health` 均為 401 JSON `AUTH_REQUIRED`。

## 公開 HTTPS 與 HP 入口

2026-10-12 05:06 起以匿名請求核對：

| 路徑／元件 | 結果 |
| --- | --- |
| `/orderflow/`、`/orderflow/test/`、`/orderflow/integration/`、`/orderflow/help/` | 200；每頁恰好一行 Cloudflare beacon 注入，移除該整行後其餘位元與固定 head 相同 |
| `/orderflow/app.js`、`manage.js`、`integration.js` | 200；原始回應位元與固定 head 完全相同 |
| 上述五個匿名 API | 401 JSON `AUTH_REQUIRED`，無 Basic Auth challenge |
| 網站根首頁 `/` | 200 |
| `/orderflowx/`、`/orderflow/help/index.html`、`/orderflow/docs/user-guide/index.html` | 404 |
| HP Caddy、cloudflared、orderflow-asus-tunnel | 均 active；HP 舊 orderflow.service inactive |
| HP loopback 根首頁／OrderFlow | 首頁 200；OrderFlow 308 至 `https://momonong.me/orderflow/`，符合 HTTPS 轉向 |

HP `/etc/caddy/Caddyfile` SHA-256 為 `0e9b6473c465309ea5fad4dde547b9f286d3a363acd69142a3bd3157505da209`，與既有入口紀錄一致。此次未改入口、路由或其他服務。

首次公開 HTML 比對失敗的原因是檢查器只接受舊的 `defer` beacon，而目前 Cloudflare 注入 `type="module"` beacon。經逐行 diff 確認差異僅為該行，檢查器才精確移除同一 Cloudflare URL 的單一 script 整行並再次通過；沒有忽略其他 HTML 差異。第一次回條保留於 `/tmp/orderflow-logout-release-12cbed79/postcheck-public.log`，修正後為 `postcheck-public-v2.log`。

## 驗證界限與保留項目

- 修復提交的本機 Python 101 項、JavaScript 14 個入口通過；額外部署閘門 6 項通過，部署前 ASUS 私有暫存 4 項登出邊界測試通過。root gate 回報 PASS，但未輸出測試詳細數字，因此不另宣稱 ASUS 完整測試數量。
- 後檢只有匿名 HTTP、原始碼／靜態檔、systemd、listener 與權限核對；沒有正式登入、上傳、Google 呼叫、資料修改、服務重啟或再次部署。
- 手機 Safari 的實際「保存 → 登出 → 重登入」仍待人工驗收。原四筆 Invoice 的存在、原 owner 與恢復方式尚未查核，不能宣稱已找回。
- cookie 仍為會話型；清除 cookie、換瀏覽器／裝置及登入回應本身遺失，不在本修復的保證內。詳見[修復紀錄](logout-record-retention-2026-10-11.md)。
- 備份、ASUS 私有 staging、分支與 worktree 均保留。舊部署文件提交 `a0fb0a81d7dcef770a2587ab2cdd525638955156` 所在分支未改動。部署後檢完成時，新增紀錄先存於本機文件分支；後續 Git 交付以對應 PR 為準。未建立 tag/release。
