import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const cache = new Map();
async function load(url) {
  if (cache.has(url.href)) return cache.get(url.href);
  let code = ts.transpileModule(await readFile(url, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  for (const match of [...code.matchAll(/from\s+"(\.[^"]+)"/g)]) {
    code = code.replaceAll(`"${match[1]}"`, `"${await load(new URL(`${match[1]}.ts`, url))}"`);
  }
  const result = `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
  cache.set(url.href, result);
  return result;
}
const { profileSourceFiles, parseCsv } = await import(await load(new URL("../app/lib/source-profile.ts", import.meta.url)));
const { localRequestError, localBackendBase } = await import(await load(new URL("../app/lib/local-boundary.ts", import.meta.url)));
const { encodeSnapshot, expiredSnapshot } = await import(await load(new URL("../app/lib/run-snapshot.ts", import.meta.url)));
const { sourceStatus } = await import(await load(new URL("../app/lib/source-status.ts", import.meta.url)));

test("failed, pending and unknown publications never display ready", () => {
  assert.equal(sourceStatus("ready"), "Ready");
  assert.equal(sourceStatus("publish_failed"), "Publish failed");
  for (const status of [undefined, "reviewed", "awaiting_validation", "uploaded", "unexpected"]) {
    assert.equal(sourceStatus(status), "Awaiting validation");
  }
});

test("CSV profile reflects actual fields and sampled missing values", async () => {
  const file = new File(['id,"label, detail",amount\n1,"a, b",10\n2,"multi\nline",\n'], "records.csv");
  const profile = await profileSourceFiles([file]);
  assert.equal(profile.sampledRows, 2);
  assert.deepEqual(profile.columns.map(column => column.name), ["id", "label, detail", "amount"]);
  assert.equal(profile.columns[2].nulls, 1);
  assert.equal(profile.columns[2].type, "NUMBER (sample)");
});
test("malformed CSV cannot become a successful physical profile", async () => {
  for (const source of ['id,id\n1,2\n', 'id,ID\n1,2\n', 'id,value\n1,2,3\n', 'id,value\n1,"open', 'id,value\n1,"closed"bad\n']) {
    await assert.rejects(profileSourceFiles([new File([source], "bad.csv")]));
  }
  assert.deepEqual(parseCsv('id,value\n1,"unfinished', true), [["id", "value"]]);
});
test("Parquet browser inspection remains explicitly unverified", async () => {
  const profile = await profileSourceFiles([new File(["not inspected"], "data.parquet")]);
  assert.equal(profile.columns.length, 0);
  assert.match(profile.warnings[0], /not inspected/);
});
test("local request and upstream boundaries reject foreign origins and hosts", () => {
  assert.equal(localRequestError(new Request("http://localhost:3000/api/studio/runs")), null);
  for (const request of [new Request("http://example.com/api"),
    new Request("http://localhost:3000/api", { headers: { origin: "https://evil.example" } }),
    new Request("http://localhost:3000/api", { headers: { "sec-fetch-site": "cross-site" } })]) {
    assert.match(localRequestError(request), /denied|loopback/);
  }
  for (const value of ["https://example.com", "http://user:pass@localhost", "file:///tmp", "http://localhost/?key=x"]) assert.throws(() => localBackendBase(value));
  assert.equal(localBackendBase("http://127.0.0.1:8000/"), "http://127.0.0.1:8000");
});
test("snapshots cannot change run identity or terminal status", () => {
  const view = { runId: "qf_test", status: "success", provenance: "live", columns: ["n"], rows: [[3]] };
  assert.deepEqual(JSON.parse(encodeSnapshot(view, "qf_test", "success")), view);
  assert.throws(() => encodeSnapshot(view, "another", "success"));
  assert.throws(() => encodeSnapshot(view, "qf_test", "blocked"));
  assert.throws(() => encodeSnapshot({ ...view, status: "unknown" }, "qf_test", "unknown"));
  assert.throws(() => encodeSnapshot({ ...view, rows: [["x".repeat(2 * 1024 * 1024)]] }, "qf_test", "success"));
});
test("expired, absent or corrupt snapshot expiry is unavailable", () => {
  for (const value of [null, "invalid", "2024-01-01T00:00:00Z"]) assert.equal(expiredSnapshot(value, Date.parse("2025-01-01")), true);
  assert.equal(expiredSnapshot("2026-01-01T00:00:00Z", Date.parse("2025-01-01")), false);
});
