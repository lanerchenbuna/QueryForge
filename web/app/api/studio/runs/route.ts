import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";

export async function GET() {
  try {
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const result = await db
      .prepare(
        `SELECT id, question, status, model, row_count, duration, created_at
         FROM studio_runs
         ORDER BY created_at DESC
         LIMIT 50`,
      )
      .all();
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
    const payload = (await request.json()) as {
      id?: string;
      question?: string;
      status?: string;
      model?: string;
      rowCount?: number;
      duration?: string;
    };
    if (!payload.id || !payload.question) {
      return Response.json(
        { detail: "Run id and question are required." },
        { status: 400 },
      );
    }
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    await db
      .prepare(
        `INSERT OR REPLACE INTO studio_runs (
          id, question, status, model, row_count, duration
        ) VALUES (?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        payload.id,
        payload.question,
        payload.status ?? "Passed",
        payload.model ?? "configured model",
        payload.rowCount ?? 0,
        payload.duration ?? "live",
      )
      .run();
    return Response.json({ status: "saved", id: payload.id }, { status: 201 });
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
