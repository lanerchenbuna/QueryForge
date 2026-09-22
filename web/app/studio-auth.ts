import type { ChatGPTUser } from "./chatgpt-auth";

const USER_EMAIL_HEADER = "oai-authenticated-user-email";
const USER_FULL_NAME_HEADER = "oai-authenticated-user-full-name";
const USER_FULL_NAME_ENCODING_HEADER =
  "oai-authenticated-user-full-name-encoding";
const PERCENT_ENCODED_UTF8 = "percent-encoded-utf-8";

export type StudioAuthMode = "off" | "chatgpt";

/**
 * Effective Studio auth mode, driven by the STUDIO_AUTH_MODE env var.
 * `off` (default) keeps the current anonymous behavior; `chatgpt` requires
 * an `oai-authenticated-user-*` header on every Studio/proxy request.
 */
export function studioAuthMode(): StudioAuthMode {
  return process.env.STUDIO_AUTH_MODE === "chatgpt" ? "chatgpt" : "off";
}

/**
 * Reads the ChatGPT-authenticated user from the incoming request headers.
 * Returns null when no user header is present (regardless of auth mode).
 */
export function getStudioUser(request: Request): ChatGPTUser | null {
  const email = request.headers.get(USER_EMAIL_HEADER);
  if (!email) return null;

  const encodedFullName = request.headers.get(USER_FULL_NAME_HEADER);
  const fullName =
    encodedFullName &&
    request.headers.get(USER_FULL_NAME_ENCODING_HEADER) === PERCENT_ENCODED_UTF8
      ? safeDecodeURIComponent(encodedFullName)
      : null;

  return {
    displayName: fullName ?? email,
    email,
    fullName,
  };
}

export type StudioAuthResult = {
  user: ChatGPTUser | null;
  response: Response | null;
};

/**
 * Guards a Studio route handler. When STUDIO_AUTH_MODE is "chatgpt" and the
 * request carries no authenticated user, returns a 401 JSON response that the
 * handler must return immediately. When mode is "off" this never blocks and
 * always returns a null response.
 */
export function requireStudioUser(request: Request): StudioAuthResult {
  if (studioAuthMode() !== "chatgpt") {
    return { user: null, response: null };
  }
  const user = getStudioUser(request);
  if (!user) {
    return {
      user: null,
      response: Response.json(
        { detail: "Authentication required." },
        { status: 401 },
      ),
    };
  }
  return { user, response: null };
}

function safeDecodeURIComponent(value: string): string | null {
  try {
    return decodeURIComponent(value);
  } catch {
    return null;
  }
}
