import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
const source = await readFile(new URL('../app/lib/publication-status.ts', import.meta.url), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ES2022 } }).outputText;
const { publicationOutcome } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('publication requires a real executable version, not HTTP success alone', () => {
  for (const payload of [null, {}, {source: {}}, {source: {pythonPublish: {status:'not_attempted'}}},
    {source: {pythonPublish: {status:'failed', detail:'duplicate primary key'}}},
    {source: {pythonPublish: {status:'published', data_version:''}}}]) {
    assert.equal(publicationOutcome(payload,true).published,false);
  }
  const good={source:{id:'source-1',pythonPublish:{status:'published',data_version:'version-1'}}};
  assert.equal(publicationOutcome(good,false).published,false);
  assert.deepEqual(publicationOutcome(good,true),{published:true,version:'version-1',sourceId:'source-1',detail:'Published data version version-1.'});
});

test('backend validation failures stay visible to the user',()=> {
  const result=publicationOutcome({source:{pythonPublish:{status:'failed',detail:'duplicate primary key'}}},false);
  assert.equal(result.detail,'duplicate primary key');
  assert.equal(result.published,false);
});
