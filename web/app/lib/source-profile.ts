/** Bounded physical CSV inspection; sample findings are never publication verdicts. */
export type SourceProfile = {
  files: number;
  sampledRows: number;
  warnings: string[];
  columns: Array<{ name: string; type: string; nulls: number }>;
};

export function parseCsv(text: string, incomplete = false, maxRows = 1001): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false, closedQuote = false;
  text = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') {
      if (quoted && text[i + 1] === '"') { cell += '"'; i++; }
      else if (quoted) { quoted = false; closedQuote = true; }
      else if (!cell && !closedQuote) quoted = true;
      else throw new Error("Malformed CSV quoting.");
    } else if (char === "," && !quoted) { row.push(cell); cell = ""; closedQuote = false; }
    else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(cell);
      if (row.some(value => value.length)) rows.push(row);
      row = []; cell = ""; closedQuote = false;
      if (rows.length >= maxRows) return rows;
    } else {
      if (closedQuote) throw new Error("Unexpected text after a CSV closing quote.");
      cell += char;
    }
  }
  if (quoted && !incomplete) throw new Error("Unclosed CSV quote.");
  if (!incomplete && (cell || row.length)) { row.push(cell); rows.push(row); }
  return rows;
}

export async function profileSourceFiles(files: File[]): Promise<SourceProfile> {
  const result: SourceProfile = { files: files.length, sampledRows: 0, warnings: [], columns: [] };
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith(".csv")) {
      result.warnings.push(`${file.name}: schema not inspected in the browser; publication must validate it.`);
      continue;
    }
    const rows = parseCsv(await file.slice(0, 2 * 1024 * 1024).text(), file.size > 2 * 1024 * 1024);
    const header = rows.shift()?.map(value => value.trim()) ?? [];
    if (!header.length || header.some(value => !value) || new Set(header.map(value => value.toLowerCase())).size !== header.length) {
      throw new Error(`${file.name}: CSV needs non-empty, unique column names.`);
    }
    if (rows.some(row => row.length !== header.length)) throw new Error(`${file.name}: inconsistent CSV row width.`);
    result.sampledRows += rows.length;
    if (file.size > 2 * 1024 * 1024 || rows.length >= 1000) result.warnings.push(`${file.name}: bounded sample only.`);
    result.columns.push(...header.map((name, index) => {
      const values = rows.map(row => row[index]);
      const nonempty = values.filter(value => value.trim() !== "");
      return { name, type: nonempty.length && nonempty.every(value => Number.isFinite(Number(value))) ? "NUMBER (sample)" : "TEXT (sample)", nulls: values.length - nonempty.length };
    }));
  }
  return result;
}
