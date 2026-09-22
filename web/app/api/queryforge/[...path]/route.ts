import { requireStudioUser } from "@/app/studio-auth";

const ALLOWED_PATHS = new Set([
  "health",
  "models",
  "skills",
  "ask",
  "ask/stream",
  "analyze",
  "plan",
]);

function isAllowedPath(parts: string[]): boolean {
  const path = parts.join("/");
  if (ALLOWED_PATHS.has(path)) return true;
  // Dynamic publish route: /domains/{domain_id}/publish (step 03).
  return (
    parts.length === 3 &&
    parts[0] === "domains" &&
    parts[2] === "publish"
  );
}

/**
 * SSE runs are long-lived by design: the client aborts them through its own
 * `AbortController` (the Cancel button), so the proxy must not impose the
 * single-response timeout on `/ask/stream`. A timeout there would truncate the
 * stream mid-run and make the Studio report "stream ended without a terminal
 * event" for a run that was still healthy.
 */
function isEventStreamPath(parts: string[]): boolean {
  return parts.join("/") === "ask/stream";
}

function backendUrl(parts: string[]) {
  const path = parts.join("/");
  if (!isAllowedPath(parts)) {
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
      signal: isEventStreamPath(path)
        ? request.signal
        : AbortSignal.any([request.signal, AbortSignal.timeout(120_000)]),
    });

    const responseHeaders = new Headers();
    responseHeaders.set(
      "content-type",
      upstream.headers.get("content-type") ?? "application/json",
    );
    responseHeaders.set("cache-control", "no-store");
    // The event protocol version is part of the contract: forward it so the
    // consumer can detect a version it does not speak instead of guessing.
    const protocol = upstream.headers.get("x-queryforge-event-protocol");
    if (protocol) {
      responseHeaders.set("x-queryforge-event-protocol", protocol);
    }

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
