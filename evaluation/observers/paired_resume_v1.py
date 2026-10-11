"""Resume an authorized pair schedule without rerunning any completed case."""
import copy
import hashlib
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from scripts import run_paired_benchmark as paired
from queryforge.evaluation import TaskOutcome, TaskTrace
from queryforge.infrastructure.models.base import BaseModelProvider

source_path = Path(sys.argv.pop(1)).resolve()
source = json.loads(source_path.read_text())
args = paired.parse_args()
preview_args = copy.copy(args)
preview_args.dry_run = True
preview = paired.run(preview_args)
manifest = preview['manifest']
for key in manifest:
    if key != 'api_spend' and manifest[key] != source['manifest'][key]:
        raise RuntimeError('Resume refused: manifest changed: '+key)
schedule = [dict(task_id=item['task_id'], repeat=item['repeat'], arm=arm)
            for item in manifest['schedule'] for arm in item['order']]
retained = source['records']
if not retained or len(retained) >= len(schedule):
    raise RuntimeError('Resume requires a nonempty incomplete schedule')
for position, record in enumerate(retained):
    if any(record[key] != value for key, value in schedule[position].items()):
        raise RuntimeError('Retained prefix does not match the schedule')
report_path = Path(args.report).resolve()
for path in [report_path, report_path.with_suffix('.checkpoint.jsonl'),
             report_path.with_suffix('.provider_usage.jsonl'), report_path.with_suffix('.manifest.json')]:
    if path.exists():
        raise RuntimeError('Resume refuses to overwrite evidence: '+str(path))
usage_path = report_path.with_suffix('.provider_usage.jsonl')
usage_file = usage_path.open('x')
usage_file.write(source_path.with_suffix('.provider_usage.jsonl').read_text())
usage_file.flush()
checkpoint = report_path.with_suffix('.checkpoint.jsonl').open('x')
for record in retained:
    checkpoint.write(json.dumps(record, ensure_ascii=False)+'\n')
checkpoint.flush()
observer = {'sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'path':str(Path(__file__).resolve()), 'resumed_from':str(source_path),
            'resumed_from_sha256':hashlib.sha256(source_path.read_bytes()).hexdigest(),
            'retained_executions':len(retained),
            'description':'Completed prefix reused verbatim, including timeout failure; only unfinished cases call the provider',
            'original_observer':source.get('execution_observer'),
            'checkpoint':str(report_path.with_suffix('.checkpoint.jsonl')),
            'provider_usage':str(usage_path)}
report_path.with_suffix('.manifest.json').write_text(json.dumps({'manifest':manifest,'observer':observer},indent=2)+'\n')
position = 0
records = list(retained)
original_usage = BaseModelProvider.record_usage
original_run_suite = paired.harness.run_suite

def observed_usage(self, raw_usage):
    normalized = original_usage(self, raw_usage)
    raw = raw_usage.model_dump() if hasattr(raw_usage, 'model_dump') else raw_usage
    raw = raw if isinstance(raw, dict) else {}
    entry = dict(schedule[position], at_utc=datetime.now(timezone.utc).isoformat(),
                 provider=getattr(self,'provider',None), model=getattr(self,'model',None))
    for key in ('prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'):
        value = raw.get(key)
        entry[key] = value if type(value) is int and value >= 0 else None
    details = raw.get('completion_tokens_details') or {}
    entry['reasoning_tokens'] = details.get('reasoning_tokens') if isinstance(details,dict) else None
    usage_file.write(json.dumps(entry,ensure_ascii=False)+'\n')
    usage_file.flush()
    return normalized

def observed_run_suite(specs, context, **kwargs):
    global position
    item = schedule[position]
    if len(specs) != 1 or specs[0].task_id != item['task_id']:
        raise RuntimeError('Observer schedule mismatch')
    if position < len(retained):
        record = retained[position]
        outcome = TaskOutcome.model_validate({key:value for key,value in record.items()
                                             if key not in {'arm','repeat','environment_error','environment_error_reason','provider_contract_error','raw_trace'}})
        position += 1
        return [outcome], [record['raw_trace']]
    print(json.dumps(dict(event='started',execution=position+1,total=len(schedule),**item)),flush=True)
    outcomes,traces = original_run_suite(specs,context,**kwargs)
    if len(outcomes) != 1 or len(traces) != 1:
        raise RuntimeError('Expected exactly one outcome and trace')
    record = outcomes[0].model_dump()
    errors = ' '.join(paired.error_text(traces[0]))
    reason = paired._environment_error_reason(RuntimeError(errors)) if not record['passed'] else None
    contract = not record['passed'] and paired.provider_contract_error(errors)
    record.update(item,environment_error=reason is not None,environment_error_reason=reason,
                  provider_contract_error=contract,raw_trace=traces[0])
    records.append(record)
    checkpoint.write(json.dumps(record,ensure_ascii=False)+'\n')
    checkpoint.flush()
    print(json.dumps(dict(event='completed',execution=position+1,total=len(schedule),**item,
                          passed=record['passed'],wall_ms=record.get('wall_ms'),
                          measured_tokens=record.get('measured_tokens'),failure_class=record.get('failure_class'))),flush=True)
    position += 1
    if reason or contract:
        raise RuntimeError('Infrastructure/provider-contract failure; preserve checkpoint')
    if not record.get('measured_tokens') and 'model request failed' in errors.casefold():
        raise RuntimeError('Provider request failed with mixed or missing measured usage; preserve checkpoint')
    return outcomes,traces

BaseModelProvider.record_usage = observed_usage
paired.harness.run_suite = observed_run_suite
try:
    report = paired.run(args)
    if report['records'][:len(retained)] != retained:
        raise RuntimeError('Retained records changed during resume')
except Exception as exc:
    report = {'benchmark':'paired_agent_tasks','manifest':manifest,'records':records,
              'executed':bool(records),'summary':{'complete':False},
              'interruption':{'type':type(exc).__name__,'reason':str(exc)}}
    code = 1
else:
    code = 0 if report['summary']['complete'] else 1
finally:
    checkpoint.close()
    usage_file.close()
report['execution_observer'] = observer
with report_path.open('x') as target:
    json.dump(report,target,ensure_ascii=False,indent=2)
    target.write('\n')
print(json.dumps({'report':str(report_path),'executions':len(records),'summary':report['summary']},ensure_ascii=False),flush=True)
raise SystemExit(code)
