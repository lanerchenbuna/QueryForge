/** Stored bytes alone are not an executable, validated publication. */
export function sourceStatus(status: unknown): "Ready" | "Publish failed" | "Awaiting validation" {
  if (status === "ready") return "Ready";
  if (status === "publish_failed") return "Publish failed";
  return "Awaiting validation";
}
