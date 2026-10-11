# 登出再登入的 Invoice 資料保留修復

狀態：本機修復與自動化回歸完成，尚未推送、合併或部署。基線為 PR #15 的合併提交 `cba7ae3690e29586c814a377ad25d6ef9a7b00a9`（程式樹與 `b85eedc5ed403e2e084f7dbcbc34d67244f55304` 相同）。既有部署紀錄提交 `a0fb0a81d7dcef770a2587ab2cdd525638955156` 留在原分支，沒有混入修復。

## 已重現的缺陷

測試使用臨時 SQLite、合成 PDF 與 loopback 真 HTTP。採購單與 Invoice 經管理上傳 API 保存 PDF，再經 record-set API 保存人工確認列；辨識來源以合成完成工作建立，沒有呼叫 Google。

1. 相同 cookie 的兩個登出請求：第一個更新 `sessions.token_hash` 並回傳新的無權限 cookie；第二個找不到舊 token，回傳 `Max-Age=0`。瀏覽器若最後套用清除 cookie 的回應，重登入就建立新的 owner。
2. 單一登出請求已成功處理，但回應／Set-Cookie 沒送達：瀏覽器仍持有舊 token，資料庫已改成另一 token；重登入同樣建立新的 owner。

修復前兩個回歸均失敗，且已證明原 owner 的 record sets 與 PDF 還在。這是資料歸屬失聯，不是登出刪除資料。此證據不能單獨證明使用者手機截圖的事故也經過相同路徑。

## 修正

- 登出在交易內刪除 `session_auth`，清除該 session 記憶體金鑰，保留 `sessions.token_hash` 作為無權限的瀏覽器識別。
- 所有登出回應都不送 Set-Cookie，包括未知、缺少或已輪換的 token。回應遺失、重送或延遲抵達，均不會覆蓋新登入的 cookie。
- 密碼重新登入仍輪換 token；登出後原 token 不能讀寫 API，輪換後也不會重新取得權限。保留識別不等於保留登入。
- 管理頁防止重複登入／登出，並以登入狀態世代忽略較早的 bootstrap／401 回應，避免登出後重新顯示舊工作區或讓舊 401 清掉新登入列表。診斷與整合頁也加入重複登入操作防護。
- 不改資料表結構、文件 owner、PDF、辨識工作或正式資料。沒有重新上傳、重建紀錄或重送 AI。

## 驗證

環境：本機 Linux、Python 3.12、專案鎖定依賴、Node.js 22。正式站與真實單據未用於修復測試。

| 場景 | 結果與證據 |
| --- | --- |
| 一般登入輪換 → 登出 → 重登入 | 同一 owner 與文件保留；記憶體 key 清除；舊 token 401 |
| 兩個相同 cookie 並行登出 | 兩個回應都無 Set-Cookie；重登入原 Invoice／採購單、修訂與 PDF 位元保留 |
| 丟棄登出回應 | 原 cookie 已無 API 權限；輸入密碼後取回原資料 |
| 保留登出回應，先重新登入再收取 | 延遲回應不改 cookie；再送舊／未知／缺 cookie 登出也不撤銷新登入 |
| 匿名／舊 token／其他 owner | 三種 bootstrap 維持 401；另一已登入 owner 列表為空且直接讀 record set 為 404 |
| 過期與資料層重新開啟 | 同一 cookie 重登入可取回紀錄；未增加辨識工作，原 PDF 位元相同 |
| 前端腳本 | VM/DOM fixture 驗證重複操作、Invoice／採購單列表重現、延遲 bootstrap、舊 401 與未知登出可重試 |
| 完整回歸 | Python **101 項通過**；JavaScript **14 個測試入口通過**；`git diff --check` 通過 |

重現／回歸入口：

```bash
uv run --locked --offline python -m unittest discover -s tests -p test_public_ai.py -k logout
uv run --locked --offline python -m unittest discover -s tests -v
node tests/test_logout_ui.cjs
```

本次原始回條暫存於 `/tmp/orderflow-logout-baseline.log`（修復前 3 項中的 2 項失敗）、`/tmp/orderflow-logout-fixed.log`（登出邊界 4 項通過）與 `/tmp/orderflow-logout-full-python.log`（101 項通過）；它們是本機診斷產物，未提交。JavaScript 迴歸包含 `tests/test_*.cjs` 與 `tests/test_*.mjs`；另於完整回歸後補驗了診斷／整合頁防重送行為，`test_logout_ui.cjs` 通過。

## 尚未證明與資料找回界線

- 尚待確認使用者是同頁立刻重登入、完整關閉瀏覽器，或換裝置後發生問題；未完成手機 Safari 人工驗收。
- cookie 仍是 session cookie，沒有新增持久 cookie。完整關閉瀏覽器可能清除它；保留 cookie 的同一會話可重開頁面，清除 cookie／換瀏覽器／跨裝置不會因相同網站密碼而獲得其他 owner 資料。測試確認缺 cookie 會建立獨立 owner，而舊資料仍留存。
- 本修復保護之後的登出操作，不能把先前已遺失的 cookie 自動找回。正式資料是否仍存在、如何確認原 owner 及恢復其存取，尚無正式資料查核證據；不能宣稱已找回使用者那四筆 Invoice，也不能直接合併所有 owner 或用備份覆寫。
- 密碼登入回應本身遺失、不同分頁同時提交密碼登入的 token 輪換，尚未納入本次保證；本次已驗證的是登出重送／遺失／延遲與重新登入交錯。
- 此版尚未推送、合併或部署。後續版本交付、持久識別或跨裝置帳號／工作區方案、正式孤兒資料恢復，各須按確認範圍接續。
