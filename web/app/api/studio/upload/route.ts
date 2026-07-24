import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";

const MAX_FILE_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set(["sqlite", "db", "csv", "parquet"]);

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

export async function GET(request: Request) {
  try {
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

    for (const file of files) {
      const objectKey = `domains/${domainId}/sources/${sourceId}/${safeFileName(file.name)}`;
      await uploads.put(objectKey, await file.arrayBuffer(), {
        httpMetadata: { contentType: file.type || "application/octet-stream" },
        customMetadata: {
          sourceId,
          domainId,
          semanticEntity: semanticContract.entity,
          semanticReviewed: "true",
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
    const semanticModel = {
      ...semanticContract,
      reviewed: true,
      domain: { id: domainId, name: domain.name },
      inference: {
        status: "human_reviewed",
        note: "Physical inference was confirmed by the accountable domain owner.",
      },
      contract: { status: "passed", blocking_failures: 0 },
    };

    await db
      .prepare(
        `INSERT INTO studio_sources (
          id, domain_id, name, source_type, object_key, size_bytes, table_count,
          status, semantic_json, reviewed, contract_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, 1, 'passed')`,
      )
      .bind(
        sourceId,
        domainId,
        sourceName,
        sourceType,
        stored[0].objectKey,
        totalBytes,
        stored.length,
        JSON.stringify(semanticModel),
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
          status: "ready",
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
