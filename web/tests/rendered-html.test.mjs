import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("builds the complete QueryForge Studio shell", async () => {
  const [page, layout, css] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(layout, /QueryForge Studio/);
  assert.match(layout, /openGraph/);
  assert.match(page, /Ask & Analyze/i);
  assert.match(page, /Semantic Studio/i);
  assert.match(page, /Data Domains/i);
  assert.match(page, /Create a data domain/i);
  assert.match(page, /370,762/);
  assert.match(css, /@media \(max-width:/);
  await access(new URL("../dist/server/index.js", import.meta.url));
  await access(new URL("../dist/client/og.png", import.meta.url));
});

test("keeps domains isolated and semantic review mandatory", async () => {
  const [page, uploadRoute, domainRoute, schema] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(
      new URL("../app/api/studio/upload/route.ts", import.meta.url),
      "utf8",
    ),
    readFile(
      new URL("../app/api/studio/domains/route.ts", import.meta.url),
      "utf8",
    ),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
  ]);

  assert.match(page, /semanticReviewed/);
  assert.match(page, /Semantic review is mandatory/i);
  assert.match(page, /Trust Trace/i);
  assert.match(page, /domain_id/);
  assert.match(uploadRoute, /form\.get\("reviewed"\) !== "true"/);
  assert.match(uploadRoute, /form\.get\("domain_id"\)/);
  assert.match(uploadRoute, /form\.get\("semantic_contract"\)/);
  assert.match(uploadRoute, /A complete semantic contract/);
  assert.match(uploadRoute, /domains\/\$\{domainId\}\/sources/);
  assert.match(uploadRoute, /reviewed semantic contract is required/i);
  assert.match(domainRoute, /INSERT INTO studio_domains/);
  assert.match(schema, /studio_domains/);
  assert.match(schema, /domain_id/);
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../public/favicon.png", import.meta.url));
});

test("hardens Studio run history and upload attribution", async () => {
  const [page, uploadRoute, runsRoute, schema, runtime, studioAuth, proxyRoute] =
    await Promise.all([
      readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
      readFile(
        new URL("../app/api/studio/upload/route.ts", import.meta.url),
        "utf8",
      ),
      readFile(
        new URL("../app/api/studio/runs/route.ts", import.meta.url),
        "utf8",
      ),
      readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
      readFile(new URL("../db/runtime.ts", import.meta.url), "utf8"),
      readFile(new URL("../app/studio-auth.ts", import.meta.url), "utf8"),
      readFile(
        new URL("../app/api/queryforge/[...path]/route.ts", import.meta.url),
        "utf8",
      ),
    ]);

  // Run persistence: never overwrite an existing run, mark demo runs.
  assert.match(runsRoute, /ON CONFLICT\(id\) DO NOTHING/);
  assert.match(runsRoute, /INSERT INTO studio_runs/);
  assert.match(runsRoute, /is_demo/);
  assert.match(runsRoute, /RUN_STATUSES/);
  assert.match(runsRoute, /crypto\.randomUUID\(\)/);
  assert.match(runsRoute, /include_demo/);
  assert.match(schema, /is_demo/);
  assert.match(runtime, /is_demo/);
  assert.match(runtime, /ALTER TABLE studio_runs/);
  assert.match(page, /isDemo/);
  assert.match(page, /reviewed_by/);

  // Upload attribution: no fabricated inference/contract verdicts.
  assert.match(uploadRoute, /reviewed_by/);
  assert.match(uploadRoute, /csv_headers_checked/);
  assert.match(uploadRoute, /not_verified_server_side/);
  assert.match(uploadRoute, /contract_status/);
  assert.doesNotMatch(uploadRoute, /human_reviewed/);

  // Auth gate is configurable via STUDIO_AUTH_MODE.
  assert.match(studioAuth, /STUDIO_AUTH_MODE/);
  assert.match(studioAuth, /Authentication required\./);
  assert.match(studioAuth, /oai-authenticated-user-email/);

  // Proxy forwards bearer/api-key credentials upstream.
  assert.match(proxyRoute, /authorization/);
  assert.match(proxyRoute, /x-api-key/);
});
