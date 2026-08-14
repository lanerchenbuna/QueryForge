import { requireStudioUser, studioAuthMode } from "@/app/studio-auth";
import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";

const MAX_FILE_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set(["sqlite", "db", "csv", "parquet"]);
const CSV_HEADER_READ_LIMIT = 256 * 1024;

type UploadedSemanticContract = {
  version: number;
  entity: string;
  description: string;
  owner: string;
  grain: string;
  primaryKey: string;
  sensitivity: "public" | "internal" | "restricted";
  dimensions: string[];
  metrics: Array<{
    name: string;
    description: string;
    aggregation: string;
    expression: string;
  }>;
};

type FileValidation = {
  file: string;
  validation_status: "csv_headers_checked" | "not_verified_server_side";
  missing_columns?: string[];
  status?: "ok";
};

const SQL_EXPRESSION_KEYWORDS = new Set([
  "select", "from", "where", "group", "by", "order", "having", "limit",
  "offset", "as", "on", "and", "or", "not", "null", "is", "in", "between",
  "like", "case", "when", "then", "else", "end", "distinct", "join", "left",
  "right", "inner", "outer", "cross", "asc", "desc", "sum", "count", "avg",
  "min", "max", "cast", "coalesce", "nullif", "if", "round", "abs", "floor",
  "ceil", "over", "partition", "rows", "range", "unbounded", "preceding",
  "following", "current", "row", "date", "datetime", "year", "month", "day",
  "hour", "strftime", "true", "false", "substr", "replace", "trim", "lower",
  "upper", "rank", "dense_rank", "row_number", "lag", "lead", "first_value",
  "last_value",
]);

function safeFileName(name: string) {
  return name.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
}

function extension(name: string) {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

function parseSemanticContract(value: FormDataEntryValue | null) {
  if (typeof value !== "string") return null;
  try {
    const contract = JSON.parse(value) as Partial<UploadedSemanticContract>;
    const required = [
      contract.entity,
      contract.description,
      contract.owner,
      contract.grain,
      contract.primaryKey,
    ];
    const sensitivityValid = ["public", "internal", "restricted"].includes(
      String(contract.sensitivity),
    );
    const dimensionsValid =
      Array.isArray(contract.dimensions) &&
      contract.dimensions.length > 0 &&
      contract.dimensions.every(
        (dimension) =>
          typeof dimension === "string" && dimension.trim().length > 0,
      );
    const metricsValid =
      Array.isArray(contract.metrics) &&
      contract.metrics.length > 0 &&
      contract.metrics.every(
        (metric) =>
          metric &&
          typeof metric.name === "string" &&
          metric.name.trim() &&
          typeof metric.description === "string" &&
          metric.description.trim() &&
          typeof metric.aggregation === "string" &&
          metric.aggregation.trim() &&
          typeof metric.expression === "string" &&
          metric.expression.trim(),
      );
    if (
      required.some(
        (field) => typeof field !== "string" || !field.trim(),
      ) ||
      !sensitivityValid ||
      !dimensionsValid ||
      !metricsValid
    ) {
      return null;
    }
    return contract as UploadedSemanticContract;
  } catch {
    return null;
  }
}

/** Bare `column` or `table.column` references inside a metric expression. */
function referencedColumns(expression: string): string[] {
  const found = new Set<string>();
  const tokens =
    expression.match(/[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?/g) ??
    [];
  for (const token of tokens) {
    const parts = token.split(".");
    const column = parts.length > 1 ? parts[parts.length - 1] : parts[0];
    if (SQL_EXPRESSION_KEYWORDS.has(column.toLowerCase())) continue;
    found.add(column);
  }
  return [...found];
}

function parseCsvHeaderLine(firstLine: string): string[] {
  const cleaned = firstLine.replace(/^\uFEFF/, "").trim();
  if (!cleaned) return [];
  return cleaned
    .split(",")
    .map((cell) => cell.trim().replace(/^"([\s\S]*)"$/, "$1"));
}

/**
 * Reads the first non-empty line of a CSV and checks that the columns named
 * by the semantic contract (grain, primary key, dimensions, and any bare
 * column references in metric expressions) exist in the header. Returns the
 * list of missing columns (empty when every referenced column is present).
 */
async function validateCsvAgainstContract(
  file: File,
  contract: UploadedSemanticContract,
): Promise<string[]> {
  const sample = await file.slice(0, CSV_HEADER_READ_LIMIT).text();
  const firstNonEmptyLine =
    sample.split(/\r?\n/).find((line) => line.trim().length > 0) ?? "";
  const header = parseCsvHeaderLine(firstNonEmptyLine);
  const headerLookup = new Set(header.map((value) => value.toLowerCase()));
  const missing = new Set<string>();
  const requiredColumns = [
    contract.grain,
    contract.primaryKey,
    ...contract.dimensions,
  ].filter((value) => Boolean(value?.trim()));
  for (const column of requiredColumns) {
    if (!headerLookup.has(column.trim().toLowerCase())) missing.add(column);
  }
  for (const metric of contract.metrics) {
    for (const column of referencedColumns(metric.expression)) {
      if (!headerLookup.has(column.toLowerCase())) missing.add(column);
    }
  }
  return [...missing];
}

export async function GET(request: Request) {
  try {
    const { response } = requireStudioUser(request);
    if (response) return response;
    const domainId = new URL(request.url).searchParams.get("domain_id");
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const statement = db.prepare(
      `SELECT id, domain_id, name, source_type, size_bytes, table_count, status,
              contract_status, created_at
       FROM studio_sources
       ${domainId ? "WHERE domain_id = ?" : ""}
       ORDER BY created_at DESC
       LIMIT 50`,
    );
    const result = domainId
      ? await statement.bind(domainId).all()
      : await statement.all();
    return Response.json({ sources: result.results });
  } catch (error) {
    return Response.json(
      {
        sources: [],
        detail:
          error instanceof Error ? error.message : "Studio storage unavailable.",
      },
      { status: 503 },
    );
  }
}

export async function POST(request: Request) {
  try {
    const auth = requireStudioUser(request);
    if (auth.response) return auth.response;
    const form = await request.formData();
    const domainId = String(form.get("domain_id") ?? "").trim();
    if (!domainId) {
      return Response.json(
        { detail: "Select a data domain before uploading data." },
        { status: 400 },
      );
    }
    if (form.get("reviewed") !== "true") {
      return Response.json(
        { detail: "A reviewed semantic contract is required." },
        { status: 400 },
      );
    }
    const reviewedBy = String(form.get("reviewed_by") ?? "").trim();
    if (!reviewedBy) {
      return Response.json(
        {
          detail:
            "An accountable reviewer (reviewed_by) is required for every publication.",
        },
        { status: 400 },
      );
    }
    if (studioAuthMode() === "chatgpt") {
      const userEmail = auth.user?.email;
      if (!userEmail || reviewedBy !== userEmail) {
        return Response.json(
          {
            detail:
              "reviewed_by must match the authenticated user's email.",
          },
          { status: 403 },
        );
      }
    }
    const semanticContract = parseSemanticContract(
      form.get("semantic_contract"),
    );
    if (!semanticContract) {
      return Response.json(
        {
          detail:
            "A complete semantic contract with entity, grain, owner, dimensions, and metrics is required.",
        },
        { status: 400 },
      );
    }

    const files = form
      .getAll("files")
      .filter((value): value is File => value instanceof File);
    if (!files.length) {
      return Response.json(
        { detail: "At least one data file is required." },
        { status: 400 },
      );
    }

    for (const file of files) {
      if (!ALLOWED_EXTENSIONS.has(extension(file.name))) {
        return Response.json(
          { detail: `Unsupported file type: ${file.name}` },
          { status: 400 },
        );
      }
      if (file.size > MAX_FILE_BYTES) {
        return Response.json(
          { detail: `${file.name} exceeds the 25 MB upload limit.` },
          { status: 413 },
        );
      }
    }

    const { db, uploads } = getStudioBindings();
    await ensureStudioSchema(db);
    const domain = await db
      .prepare("SELECT id, name FROM studio_domains WHERE id = ? LIMIT 1")
      .bind(domainId)
      .first<{ id: string; name: string }>();
    if (!domain) {
      return Response.json(
        { detail: "The selected data domain does not exist." },
        { status: 404 },
      );
    }
    const sourceId = crypto.randomUUID();
    const stored: Array<{ name: string; objectKey: string; size: number }> = [];

    // Honest server-side validation: never fabricate an inference/contract
    // verdict. CSV files get their header columns checked against the
    // semantic contract; binary files can only be marked not-verified.
    const fileValidations: FileValidation[] = [];
    let allHeadersPass = true;
    for (const file of files) {
      if (extension(file.name) === "csv") {
        const missingColumns = await validateCsvAgainstContract(
          file,
          semanticContract,
        );
        const validation: FileValidation = {
          file: file.name,
          validation_status: "csv_headers_checked",
          missing_columns: missingColumns,
        };
        if (missingColumns.length === 0) {
          validation.status = "ok";
        } else {
          allHeadersPass = false;
        }
        fileValidations.push(validation);
      } else {
        fileValidations.push({
          file: file.name,
          validation_status: "not_verified_server_side",
        });
        allHeadersPass = false;
      }
    }

    for (const file of files) {
      const objectKey = `domains/${domainId}/sources/${sourceId}/${safeFileName(file.name)}`;
      await uploads.put(objectKey, await file.arrayBuffer(), {
        httpMetadata: { contentType: file.type || "application/octet-stream" },
        customMetadata: {
          sourceId,
          domainId,
          semanticEntity: semanticContract.entity,
          semanticReviewed: "true",
          reviewedBy,
        },
      });
      stored.push({ name: file.name, objectKey, size: file.size });
    }

    const totalBytes = stored.reduce((sum, file) => sum + file.size, 0);
    const sourceName =
      stored.length === 1
        ? stored[0].name
        : `${stored[0].name} + ${stored.length - 1}`;
    const sourceType = Array.from(
      new Set(stored.map((file) => extension(file.name).toUpperCase())),
    ).join(" · ");
    const contractStatus =
      allHeadersPass && form.get("reviewed") === "true"
        ? "reviewed"
        : "review_pending";
    const sourceStatus =
      contractStatus === "reviewed" ? "ready" : "awaiting_validation";
    const semanticModel = {
      ...semanticContract,
      domain: { id: domainId, name: domain.name },
      review: { claimed: form.get("reviewed") === "true", reviewed_by: reviewedBy },
      validation: { files: fileValidations },
    };

    await db
      .prepare(
        `INSERT INTO studio_sources (
          id, domain_id, name, source_type, object_key, size_bytes, table_count,
          status, semantic_json, reviewed, contract_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        sourceId,
        domainId,
        sourceName,
        sourceType,
        stored[0].objectKey,
        totalBytes,
        stored.length,
        sourceStatus,
        JSON.stringify(semanticModel),
        contractStatus === "reviewed" ? 1 : 0,
        contractStatus,
      )
      .run();

    return Response.json(
      {
        source: {
          id: sourceId,
          domainId,
          name: sourceName,
          sourceType,
          sizeBytes: totalBytes,
          tableCount: stored.length,
          status: sourceStatus,
          contractStatus,
          semanticModel,
        },
      },
      { status: 201 },
    );
  } catch (error) {
    return Response.json(
      {
        detail:
          error instanceof Error ? error.message : "Upload publication failed.",
      },
      { status: 503 },
    );
  }
}
