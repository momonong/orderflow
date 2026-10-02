// Document-to-document quantity allocation only; no shipment, stock or payment inference.
const SCALE = 10000n;
function scaled(value) {
  if (typeof value !== "string" || !/^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,4})?$/.test(value)) return null;
  const [whole, decimal = ""] = value.split(".");
  return BigInt(whole) * SCALE + BigInt(decimal.padEnd(4, "0"));
}
function shown(value) {
  const whole = value / SCALE, fraction = String(value % SCALE).padStart(4, "0").replace(/0+$/, "");
  return `${whole}${fraction ? `.${fraction}` : ""}`;
}
export function summarizeItems(documents, links) {
  const byId = new Map(documents.map(doc => [doc.id, doc]));
  return documents.flatMap(doc => doc.confirmed.rows.map(item => {
    const related = links.filter(link => doc.kind === "invoice" ?
      link.invoice_document_id === doc.id && link.invoice_item_id === item.id :
      link.po_document_id === doc.id && link.po_item_id === item.id);
    const quantity = scaled(item.quantity);
    let unknown = quantity === null || !item.unit;
    let allocated = 0n;
    for (const link of related) {
      const otherId = doc.kind === "invoice" ? link.po_document_id : link.invoice_document_id;
      const otherItemId = doc.kind === "invoice" ? link.po_item_id : link.invoice_item_id;
      const other = byId.get(otherId)?.confirmed.rows.find(row => row.id === otherItemId);
      const amount = scaled(link.quantity);
      if (!other || !other.unit || other.unit !== item.unit || amount === null) unknown = true;
      else allocated += amount;
    }
    if (!unknown && allocated > quantity) unknown = true;
    return {documentId: doc.id, itemId: item.id, kind: doc.kind,
      number: doc.confirmed.header.number, code: item.code, description: item.description,
      unit: item.unit, allocated: unknown ? null : shown(allocated),
      remaining: unknown ? null : shown(quantity - allocated),
      status: unknown ? "未知／不可比" : !related.length ? "未對應" :
        allocated === quantity ? "已完全分配" : "部分對應"};
  }));
}
