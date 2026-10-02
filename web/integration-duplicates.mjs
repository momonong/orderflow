// Advisory only: a matching source or header needs human review, not automatic rejection.
export function suspectedDuplicates(documents, selectedId, kind, fields, fileSha) {
  const company = fields.header.company.trim().toLocaleLowerCase();
  const number = fields.header.number.trim().toLocaleLowerCase();
  return documents.flatMap(doc => {
    if (doc.id === selectedId) return [];
    const reasons = [];
    if (fileSha && doc.file_sha256 === fileSha) reasons.push("相同原檔雜湊");
    const header = doc.confirmed.header;
    if (company && number && doc.kind === kind &&
        header.company.trim().toLocaleLowerCase() === company &&
        header.number.trim().toLocaleLowerCase() === number) reasons.push("同種類／公司／單號");
    return reasons.length ? [{doc, reasons}] : [];
  });
}
