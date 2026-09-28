# 本機驗證紀錄 v0.1

環境：2026-09-28，Ubuntu/Linux、Python 3.12.3、uv 0.9.5、pypdf 6.10.2、Node.js 與 Codex in-app Chromium 153。驗證對象為 `feat/company-feasibility-check` 工作分支；未部署。

## 自動化

- `uv run --locked python -m unittest discover -s tests -v`：7 個 HTTP／持久化整合測試通過。涵蓋 `/orderflow/` 靜態資源與 JSON、session 隔離、有效／無效／超限 PDF、大小與 SHA-256 不一致、並發同鍵上傳／派工、明確重新辨識、模擬成功／AI 失敗／逾時結果不明／格式失敗、重啟後未知狀態、Host 拒絕，以及上傳／JSON 不完整請求的時限。
- `node tests/test_report.cjs`：報告不包含注入的檔名／原始結果／秘密標記，成功複製內容與畫面狀態一致，剪貼簿失敗時選取 textarea 並提示手動複製。
- `node --check web/app.js`、`uv lock --check --offline`：通過。

## 真實瀏覽器

在 loopback `http://127.0.0.1:18765/orderflow/`：頁面、CSS、JavaScript 與同站 API 正常，固定表格顯示 `中文 <測試> & "引號"`；選檔後明確確認才上傳。第一輪確認回執 SHA-256 一致、模擬成功結果表格、安全文字呈現、重新整理找回文件與工作、故障顯示 `AI_UNAVAILABLE`，並在畫面顯示報告複製成功。最後以最新 PDF 結構驗證版本再次上傳有效合成 PDF，回執為 470 bytes、1 頁，報告無合成 PDF 的 metadata 標記。瀏覽器的剪貼簿讀取工具回傳空值，因此剪貼簿內容以獨立 Node 測試確認；不將 UI 成功訊息當成外部貼上驗證。

測試伺服器只綁 `127.0.0.1`；瀏覽器測試結束後已停止。瀏覽器測試使用的 PDF 為合成資料，不含真實客戶內容。

## 尚未驗證

真實外部 AI API／模型品質、公開 HTTPS 與反向代理、公司瀏覽器／網路、公司資料外傳許可、正式登入、備份與保留政策。若整站載入失敗，頁面不可能自行產報告，需由人提供錯誤截圖與發生時間。表姐在公司端的人工驗收仍待安排。
