import * as pdfjs from "./vendor/pdfjs/pdf.min.mjs";
import {parseKoyaText} from "./local-pdf-core.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/orderflow/vendor/pdfjs/pdf.worker.min.mjs";
const allowedErrors = new Set(["LOCAL_PAGE_COUNT_UNSUPPORTED", "LOCAL_PAGE_SIZE_UNSUPPORTED",
  "LOCAL_TEXT_UNAVAILABLE", "LOCAL_TEMPLATE_UNSUPPORTED", "LOCAL_LAYOUT_UNSUPPORTED",
  "LOCAL_ROWS_UNSUPPORTED", "LOCAL_NUMBER_UNSUPPORTED", "LOCAL_DATE_UNSUPPORTED",
  "LOCAL_ROW_TOTAL_MISMATCH", "LOCAL_DOCUMENT_TOTAL_MISMATCH"]);

self.onmessage = async event => {
  let loading;
  try {
    const bytes = event.data?.bytes;
    if (!(bytes instanceof ArrayBuffer) || bytes.byteLength < 1 || bytes.byteLength > 8 * 1024 * 1024)
      throw Object.assign(new Error("LOCAL_FILE_SIZE"), {code: "LOCAL_FILE_SIZE"});
    loading = pdfjs.getDocument({data: new Uint8Array(bytes), stopAtErrors: true,
      disableAutoFetch: true, disableStream: true});
    const pdf = await loading.promise;
    if (pdf.numPages !== 1) throw Object.assign(new Error("LOCAL_PAGE_COUNT_UNSUPPORTED"),
      {code: "LOCAL_PAGE_COUNT_UNSUPPORTED"});
    const page = await pdf.getPage(1);
    const content = await page.getTextContent();
    const result = parseKoyaText({items: content.items.map(item => ({str: item.str,
      x: item.transform[4], y: item.transform[5]})), pageCount: pdf.numPages,
      pageWidth: page.view[2] - page.view[0], pageHeight: page.view[3] - page.view[1]});
    self.postMessage({ok: true, result});
  } catch (error) {
    self.postMessage({ok: false, code: allowedErrors.has(error?.code) ? error.code : "LOCAL_PDF_UNREADABLE"});
  } finally {
    if (loading) await loading.destroy().catch(() => {});
  }
};
