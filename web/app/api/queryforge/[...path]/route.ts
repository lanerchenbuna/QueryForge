import { requireStudioUser } from "@/app/studio-auth";

const ALLOWED_PATHS = new Set([
  "health",
  "models",
  "skills",
  "ask",
  "ask/stream",
  "plan",
]);

function backendUrl(parts: string[]) {
  const path = parts.join("/");
  if (!ALLOWED_PATHS.has(path)) {
    throw new Error("Unsupported QueryForge API route.");
  }
  const base = (
    process.env.QUERYFORGE_API_URL ?? "http://127.0.0.1:8000"
  ).replace(/\/+$/, "");
  return `${base}/${path}`;
}

async function proxy(
  request: Request,
  context: { params: Promise<{ path: string[] }> },
) {
  const { response } = requireStudioUser(request);
  if (response) return response;
  try {
    const { path } = await context.params;
    const target = backendUrl(path);
    const headers = new Headers();
    const contentType = request.headers.get("content-type");
    if (contentType) headers.set("content-type", contentType);
    const authorization = request.headers.get("authorization");
    if (authorization) headers.set("authorization", authorization);
    const apiKey = request.headers.get("x-api-key");
    if (apiKey) headers.set("x-api-key", apiKey);

    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : await request.arrayBuffer(),
      signal: AbortSignal.timeout(120_000),
    });

    const responseHeaders = new Headers();
    responseHeaders.set(
      "content-type",
      upstream.headers.get("content-type") ?? "application/json",
    );
    responseHeaders.set("cache-control", "no-store");

    return new Response(upstream.body, {
      status: upstream.status,
      headers: responseHeaders,
    });
  } catch (error) {
    return Response.json(
      {
        status: "unavailable",
        detail:
          error instanceof Error
            ? error.message
            : "QueryForge backend unavailable.",
      },
      { status: 503 },
    );
  }
}

export const GET = proxy;
export const POST = proxy;
