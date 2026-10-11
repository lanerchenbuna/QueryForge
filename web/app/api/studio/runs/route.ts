import { requireStudioUser } from "@/app/studio-auth";
import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";
import { encodeSnapshot, expiredSnapshot, SNAPSHOT_RETENTION_DAYS } from "@/app/lib/run-snapshot";

const RUN_STATUSES = new Set([
  "success",
  "partial",
  "failed",
  "blocked",
  "cancelled",
  "planned",
]);
const RUN_ID_PATTERN =
  /^(qf_[a-f0-9]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i;
const MAX_QUESTION_LENGTH = 8000;
const MAX_MODEL_LENGTH = 200;
const MAX_DURATION_LENGTH = 100;
const MAX_ID_LENGTH = 64;

export async function GET(request: Request) {
  try {
    const { response } = requireStudioUser(request);
    if (response) return response;
    const url = new URL(request.url);
    const domainId = url.searchParams.get("domain_id");
    const includeDemo = url.searchParams.get("include_demo") === "1";
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const runId = url.searchParams.get("run_id");
    if (runId) {
      if (!domainId) return Response.json({ detail: "Replay requires a data domain." }, { status: 400 });
      const saved = await db.prepare("SELECT domain_id, output_json, data_version, expires_at FROM studio_runs WHERE id = ? AND domain_id = ?")
        .bind(runId, domainId).first<{ domain_id: string; output_json: string | null; data_version: string | null; expires_at: string | null }>();
      if (!saved) return Response.json({ detail: "Run not found in this data domain." }, { status: 404 });
      if (!saved.output_json || expiredSnapshot(saved.expires_at)) return Response.json({ detail: "Run snapshot expired or was not retained." }, { status: 410 });
      return Response.json({ run: JSON.parse(saved.output_json), data_version: saved.data_version }, { headers: { "cache-control": "no-store" } });
    }
    const conditions: string[] = [];
    const params: string[] = [];
    if (domainId) {
      conditions.push("domain_id = ?");
      params.push(domainId);
    }
    if (!includeDemo) {
      conditions.push("is_demo = 0");
    }
    const where = conditions.length ? ` WHERE ${conditions.join(" AND ")}` : "";
    const statement = db.prepare(
      `SELECT id, domain_id, question, status, model, row_count, duration,
              created_at, is_demo, data_version
       FROM studio_runs${where}
       ORDER BY created_at DESC
       LIMIT 50`,
    );
    const result = params.length
      ? await statement.bind(...params).all()
      : await statement.all();
    return Response.json({ runs: result.results });
  } catch (error) {
    return Response.json(
      {
        runs: [],
        detail:
          error instanceof Error ? error.message : "Run history unavailable.",
      },
      { status: 503 },
    );
  }
}

export async function POST(request: Request) {
  try {
    const { response } = requireStudioUser(request);
    if (response) return response;
    const payload = (await request.json()) as {
      id?: string;
      domainId?: string;
      question?: string;
      status?: string;
      model?: string;
      rowCount?: number;
      duration?: string;
      isDemo?: boolean;
      snapshot?: unknown;
      dataVersion?: string;
    };
    const domainId = payload.domainId?.trim() ?? "";
    const question = payload.question?.trim() ?? "";
    if (!domainId || !question) {
      return Response.json(
        { detail: "Run data domain and question are required." },
        { status: 400 },
      );
    }
    const status = payload.status ?? "";
    if (!RUN_STATUSES.has(status)) {
      return Response.json(
        {
          detail: `Run status must be one of: ${[...RUN_STATUSES].join(", ")}.`,
        },
        { status: 400 },
      );
    }
    if (question.length > MAX_QUESTION_LENGTH) {
      return Response.json(
        { detail: `Question exceeds the ${MAX_QUESTION_LENGTH} character limit.` },
        { status: 400 },
      );
    }
    const model = payload.model?.trim() ?? "configured model";
    const duration = payload.duration?.trim() ?? "live";
    if (model.length > MAX_MODEL_LENGTH) {
      return Response.json(
        { detail: `Model exceeds the ${MAX_MODEL_LENGTH} character limit.` },
        { status: 400 },
      );
    }
    if (duration.length > MAX_DURATION_LENGTH) {
      return Response.json(
        { detail: `Duration exceeds the ${MAX_DURATION_LENGTH} character limit.` },
        { status: 400 },
      );
    }

    let id = payload.id?.trim() ?? "";
    if (id) {
      if (id.length > MAX_ID_LENGTH || !RUN_ID_PATTERN.test(id)) {
        return Response.json(
          {
            detail:
              "Run id must be a qf_ identifier (qf_ followed by 32 hex digits) or a UUID.",
          },
          { status: 400 },
        );
      }
    } else {
      id = crypto.randomUUID();
    }
    let snapshot: string | null;
    try { snapshot = encodeSnapshot(payload.snapshot, id, status); }
    catch (error) { return Response.json({ detail: String(error) }, { status: 400 }); }
    const dataVersion = (payload.dataVersion ?? "").slice(0, 200);
    const expiresAt = snapshot ? new Date(Date.now() + SNAPSHOT_RETENTION_DAYS * 86400_000).toISOString() : null;

    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const domain = await db
      .prepare("SELECT id FROM studio_domains WHERE id = ? LIMIT 1")
      .bind(domainId)
      .first<{ id: string }>();
    if (!domain) {
      return Response.json(
        { detail: "The data domain for this run does not exist." },
        { status: 404 },
      );
    }

    await db
      .prepare(
        `INSERT INTO studio_runs (
          id, domain_id, question, status, model, row_count, duration, is_demo,
          output_json, data_version, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING`,
      )
      .bind(
        id,
        domainId,
        question,
        status,
        model,
        payload.rowCount ?? 0,
        duration,
        payload.isDemo === true ? 1 : 0,
        snapshot, dataVersion, expiresAt,
      )
      .run();
    const saved = await db.prepare("SELECT domain_id FROM studio_runs WHERE id = ?").bind(id).first<{domain_id: string}>();
    if (saved?.domain_id !== domainId) return Response.json({ detail: "Run id already belongs to another data domain." }, { status: 409 });
    return Response.json({ status: "saved", id }, { status: 201 });
  } catch (error) {
    return Response.json(
      {
        detail:
          error instanceof Error ? error.message : "Run persistence failed.",
      },
      { status: 503 },
    );
  }
}
