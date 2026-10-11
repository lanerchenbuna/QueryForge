/** Local single-user Studio boundary; no new identity system. */
export function isLoopback(hostname: string): boolean {
  return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(hostname.toLowerCase());
}

export function localRequestError(request: Request): string | null {
  const url = new URL(request.url);
  if (!isLoopback(url.hostname)) return "Local Studio accepts loopback requests only.";
  const origin = request.headers.get("origin");
  if (origin && origin !== url.origin) return "Cross-origin local Studio request denied.";
  if (request.headers.get("sec-fetch-site") === "cross-site") return "Cross-site local Studio request denied.";
  return null;
}

export function localBackendBase(value = "http://127.0.0.1:8000"): string {
  const url = new URL(value);
  if (!isLoopback(url.hostname) || !["http:", "https:"].includes(url.protocol)
      || url.username || url.password || url.search || url.hash) {
    throw new Error("Local Studio backend must be an HTTP loopback URL without credentials, query or fragment.");
  }
  return value.replace(/\/+$/, "");
}
