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
  assert.match(page, /370,762/);
  assert.match(css, /@media \(max-width:/);
  await access(new URL("../dist/server/index.js", import.meta.url));
  await access(new URL("../dist/client/og.png", import.meta.url));
});

test("keeps semantic review mandatory and ships branded assets", async () => {
  const [page, uploadRoute] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(
      new URL("../app/api/studio/upload/route.ts", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(page, /semanticReviewed/);
  assert.match(page, /Semantic review is mandatory/i);
  assert.match(page, /Trust Trace/i);
  assert.match(uploadRoute, /form\.get\("reviewed"\) !== "true"/);
  assert.match(uploadRoute, /reviewed semantic contract is required/i);
  await access(new URL("../public/og.png", import.meta.url));
  await access(new URL("../public/favicon.png", import.meta.url));
});
