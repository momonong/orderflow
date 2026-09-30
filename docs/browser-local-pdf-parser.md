# 瀏覽器端採購憑單解析候選

狀態：本機開發與驗證候選；尚未推送、合併或部署。原有 Google AI 辨識入口保留。

## 使用範圍與資料流

- 「本機解析（不使用 AI）」只接受有文字層、A4、單頁、最多 8 MiB 的科雅「採購憑單」版型。掃描圖、其他單據、欄位位置變化、缺欄、重複品號、品項／小計／稅額／總計不一致時顯示可讀錯誤，不自動改走 Google 或 OCR。
- 選檔與解析時，瀏覽器使用同源的 PDF.js 與本機 Worker 讀取 `File.arrayBuffer()`；沒有 PDF 上傳或外部 API 請求。解析結果是候選，可編修，空白單位維持 `null`，預進貨日與備註僅作來源提示，未推定貿易條件或出貨日。
- 按「確認並儲存記錄」且通過欄位驗證後，才把原 PDF 上傳至 OrderFlow 網站主機，接著保存解析候選來源與人工確認記錄。此流程不建立 Gemini 工作、不送 Google。主機上的來源契約為 `management_local_sources`，保存解析器版本及初始候選；`management_record_sets` 以 `source_local_id` 或 `source_job_id` 二選一指向來源。客戶端來源聲稱不等於主機獨立認證了版型；主機仍驗證登入工作區、文件類型／頁數、欄位格式、來源與修訂。
- 同一請求識別重送為冪等；來源或修訂衝突不覆蓋既有資料。失敗不自動重送，頁面保留未存編修，使用者可核對文件列表再決定是否重試。已有 Gemini 記錄的文件不能再建立另一個本機來源。

## 驗證範圍

- 真實樣本只在本機以 Chrome 與 PDF.js 讀取，**沒有上傳、加入 Git 或送至 Google**：8 筆候選、數量合計 517、USD 309860.00、1 筆單位空白；備註中的月份未變成訂單日期。
- 完全合成的文字層 PDF 經 Chrome 解析後，雙擊確認只建立一組文件、來源與修訂 1 記錄。解析階段的 Chrome 網路事件只有同源 Worker 載入；確認後才有三個管理 API 請求。合成保存斷線後編修留在頁面；若 PDF 與候選已上傳而記錄 PUT 失敗，重新載入可從來源接續，未自動重送。已存記錄的後續編修斷線後，重新載入回到上次已存版本。
- Python `unittest discover` 62 項、管理頁及解析器 Node 測試通過；測試包含舊 Gemini 記錄遷移、會話隔離、來源衝突、冪等、空白單位、錯誤合計與版型漂移。未做真實 Google 呼叫、ASUS 部署、其他 PDF 版型或人工驗收。

## 第三方元件

`web/vendor/pdfjs/` 包含官方 `pdfjs-dist` 6.3.289 的 `pdf.min.mjs`、`pdf.worker.min.mjs` 與 Apache-2.0 `LICENSE`。來源：<https://www.npmjs.com/package/pdfjs-dist/v/6.3.289>；npm tarball SHA-256 `06f25e887adc6489f04c9fcb14198c77e4e5623a59a0bba5c4cea5838a4f1241`。兩個 vendored 構件 SHA-256 分別為 `f80490490320511e5df18c580b9edd6b5db8058dceebaf6f161992e0a964b9e2`、`8ab0e5e30031b4a06ecfddd5ae9562f0227f830ee7ec9ed1a968b134243d2386`。不使用 CDN。
