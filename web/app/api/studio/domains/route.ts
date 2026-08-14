import { requireStudioUser } from "@/app/studio-auth";
import { ensureStudioSchema, getStudioBindings } from "@/db/runtime";

function slugify(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

export async function GET(request: Request) {
  try {
    const { response } = requireStudioUser(request);
    if (response) return response;
    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const result = await db
      .prepare(
        `SELECT
          d.id,
          d.name,
          d.slug,
          d.description,
          d.owner,
          d.status,
          d.is_sample,
          d.created_at,
          d.updated_at,
          COUNT(DISTINCT s.id) AS source_count,
          COUNT(DISTINCT r.id) AS run_count
        FROM studio_domains d
        LEFT JOIN studio_sources s ON s.domain_id = d.id
        LEFT JOIN studio_runs r ON r.domain_id = d.id
        GROUP BY d.id
        ORDER BY d.is_sample DESC, d.created_at ASC`,
      )
      .all();
    return Response.json({ domains: result.results });
  } catch (error) {
    return Response.json(
      {
        domains: [],
        detail:
          error instanceof Error ? error.message : "Domain storage unavailable.",
      },
      { status: 503 },
    );
  }
}

export async function POST(request: Request) {
  try {
    const auth = requireStudioUser(request);
    if (auth.response) return auth.response;
    const payload = (await request.json()) as {
      name?: string;
      description?: string;
      owner?: string;
      ownerEmail?: string;
    };
    const name = payload.name?.trim() ?? "";
    if (name.length < 2 || name.length > 80) {
      return Response.json(
        { detail: "Domain name must contain 2–80 characters." },
        { status: 400 },
      );
    }

    const { db } = getStudioBindings();
    await ensureStudioSchema(db);
    const id = `domain_${crypto.randomUUID().replaceAll("-", "")}`;
    const slugBase = slugify(name) || "data-domain";
    const slug = `${slugBase}-${id.slice(-6)}`;
    const description =
      payload.description?.trim() ||
      "A governed data domain for source ingestion, semantic modeling, and trusted analysis.";
    const owner =
      payload.owner?.trim() || payload.ownerEmail?.trim() || "Workspace admin";

    await db
      .prepare(
        `INSERT INTO studio_domains (
          id, name, slug, description, owner, status, is_sample
        ) VALUES (?, ?, ?, ?, ?, 'draft', 0)`,
      )
      .bind(id, name, slug, description, owner)
      .run();

    return Response.json(
      {
        domain: {
          id,
          name,
          slug,
          description,
          owner,
          status: "draft",
          isSample: false,
          sourceCount: 0,
          runCount: 0,
        },
      },
      { status: 201 },
    );
  } catch (error) {
    return Response.json(
      {
        detail:
          error instanceof Error ? error.message : "Domain creation failed.",
      },
      { status: 503 },
    );
  }
}
