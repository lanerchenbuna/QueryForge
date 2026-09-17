/** Publication succeeds only after Python confirms an executable data version. */
export function publicationOutcome(payload: unknown, httpOk: boolean) {
  const body = payload && typeof payload === "object" ? payload as Record<string, unknown> : {};
  const source = body.source && typeof body.source === "object" ? body.source as Record<string, unknown> : {};
  const publication = source.pythonPublish && typeof source.pythonPublish === "object"
    ? source.pythonPublish as Record<string, unknown> : {};
  const version = typeof publication.data_version === "string" ? publication.data_version.trim() : "";
  const published = httpOk && publication.status === "published" && version.length > 0;
  return {
    published,
    version,
    sourceId: typeof source.id === "string" ? source.id : "",
    detail: published ? `Published data version ${version}.`
      : String(body.detail ?? publication.detail ?? "Data was not published. Check the backend and validation results, then retry."),
  };
}
