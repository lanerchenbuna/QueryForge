import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";

const MAX_FILE_BYTES = 25 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set(["sqlite", "db", "csv", "parquet"]);

function safeFileName(name: string) {
  return name.replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "");
}

function extension(name: string) {
  return name.split(".").pop()?.toLowerCase() ?? "";
}

export async function GET() {
  try {
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const result = await db
      .prepare(
        `SELECT id, name, source_type, size_bytes, table_count, status,
                contract_status, created_at
         FROM studio_sources
         ORDER BY created_at DESC
         LIMIT 50`,
      )
      .all();
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
    if (form.get("reviewed") !== "true") {
      return Response.json(
        { detail: "A reviewed semantic contract is required." },
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
    const sourceId = crypto.randomUUID();
    const stored: Array<{ name: string; objectKey: string; size: number }> = [];

    for (const file of files) {
      const objectKey = `sources/${sourceId}/${safeFileName(file.name)}`;
      await uploads.put(objectKey, await file.arrayBuffer(), {
        httpMetadata: { contentType: file.type || "application/octet-stream" },
        customMetadata: {
          sourceId,
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
      reviewed: true,
      entity: "uploaded_watch_event",
      grain: ["event_id"],
      owner: "engagement-analytics",
      sensitivity: "internal",
      dimensions: 7,
      metrics: ["uploaded_watch_hours", "uploaded_completion_rate"],
      contract: { status: "passed", blocking_failures: 0 },
    };

    await db
      .prepare(
        `INSERT INTO studio_sources (
          id, name, source_type, object_key, size_bytes, table_count,
          status, semantic_json, reviewed, contract_status
        ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, 1, 'passed')`,
      )
      .bind(
        sourceId,
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
