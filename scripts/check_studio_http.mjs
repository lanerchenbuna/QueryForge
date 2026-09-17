// Local HTTP acceptance. Requires the offline demo API and Studio dev server.
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';

const base=process.env.STUDIO_TEST_URL ?? 'http://localhost:3000';
assert.match(base,/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/,'Only local demo servers are supported');
const home=await fetch(base);assert.equal(home.status,200);
const health=await fetch(`${base}/api/queryforge/health`);assert.equal(health.status,200,await health.text());
const created=await fetch(`${base}/api/studio/domains`,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({name:`Step17 acceptance ${Date.now()}`,owner:'local acceptance'})});
assert.equal(created.status,201,await created.clone().text());
const {domain}=await created.json();
const contract={entity:'items',description:'Local acceptance catalog',owner:'local acceptance',grain:'id',primaryKey:'id',
  sensitivity:'internal',dimensions:['id','name'],metrics:[{name:'item_count',description:'Row count',aggregation:'count',expression:'COUNT(items.id)'}]};
async function upload(csv) {
  const form=new FormData();
  form.set('domain_id',domain.id);form.set('reviewed','true');form.set('reviewed_by','local-workspace');
  form.set('semantic_contract',JSON.stringify(contract));form.append('files',new Blob([csv],{type:'text/csv'}),'items.csv');
  const response=await fetch(`${base}/api/studio/upload`,{method:'POST',body:form});
  return {status:response.status,body:await response.json()};
}
async function ask() {
  const response=await fetch(`${base}/api/queryforge/ask`,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({question:'item_count',domain_id:domain.id,complexity_mode:'simple',history_top_k:0})});
  return {status:response.status,body:await response.json()};
}
const first=await upload('id,name\n1,one\n2,two\n');
assert.equal(first.status,201,JSON.stringify(first));
assert.equal(first.body.source.pythonPublish.status,'published');
const initial=await ask();assert.equal(initial.status,200,JSON.stringify(initial));assert.deepEqual(initial.body.rows,[[2]]);
const bad=await upload('id,name\n1,one\n1,duplicate\n');
assert.equal(bad.status,422,JSON.stringify(bad));assert.equal(bad.body.source.pythonPublish.status,'failed');
assert.deepEqual((await ask()).body.rows,[[2]],'Rejected data must not replace published data');
const fixed=await upload('id,name\n1,one\n2,two\n3,three\n');
assert.equal(fixed.status,201,JSON.stringify(fixed));
const answer=await ask();assert.equal(answer.status,200,JSON.stringify(answer));assert.deepEqual(answer.body.rows,[[3]]);
const missing=await fetch(`${base}/api/queryforge/ask`,{method:'POST',headers:{'content-type':'application/json'},
  body:JSON.stringify({question:'item_count',domain_id:'nonexistent_acceptance_domain'})});
assert.ok(missing.status>=400,'Unknown domain must not return demo success');
const report={mode:'real_studio_http_scripted_model',browser_ui:'not_run',domain_id:domain.id,
  first_status:first.status,rejected_status:bad.status,corrected_status:fixed.status,
  initial_rows:initial.body.rows,final_rows:answer.body.rows,unknown_domain_status:missing.status};
if(process.env.STUDIO_TEST_REPORT) await writeFile(process.env.STUDIO_TEST_REPORT,JSON.stringify(report,null,2));
console.log(JSON.stringify(report,null,2));
