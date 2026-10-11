"""Observe the existing paired harness; API parameters and verdicts are unchanged."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from datetime import datetime, timezone

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from scripts import run_paired_benchmark as paired
from queryforge.infrastructure.models.base import BaseModelProvider

args = paired.parse_args()
preview_args = copy.copy(args)
preview_args.dry_run = True
preview = paired.run(preview_args)
manifest = preview['manifest']
schedule = [dict(task_id=item['task_id'], repeat=item['repeat'], arm=arm)
            for item in manifest['schedule'] for arm in item['order']]
report_path = Path(args.report).resolve()
report_path.parent.mkdir(parents=True, exist_ok=True)
checkpoint_path = report_path.with_suffix('.checkpoint.jsonl')
usage_path = report_path.with_suffix('.provider_usage.jsonl')
manifest_path = report_path.with_suffix('.manifest.json')
observer = {'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'path': str(Path(__file__).resolve()),
            'description': 'Progress/checkpoint and numeric provider-usage observer; no API parameter, task or verdict changes',
            'checkpoint': str(checkpoint_path), 'provider_usage': str(usage_path)}
manifest_path.write_text(json.dumps({'manifest':manifest,'observer':observer}, ensure_ascii=False, indent=2)+'\n')
checkpoint = checkpoint_path.open('x')
usage_file = usage_path.open('x')
lock = threading.Lock()
position = 0
records = []
original_usage = BaseModelProvider.record_usage
original_run_suite = paired.harness.run_suite

def observed_usage(self, raw_usage):
    normalized = original_usage(self, raw_usage)
    raw = raw_usage.model_dump() if hasattr(raw_usage, 'model_dump') else raw_usage
    raw = raw if isinstance(raw, dict) else {}
    entry = dict(schedule[position], at_utc=datetime.now(timezone.utc).isoformat(),
                 provider=getattr(self, 'provider', None), model=getattr(self, 'model', None))
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens',
                'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens'):
        value = raw.get(key)
        entry[key] = value if type(value) is int and value >= 0 else None
    details = raw.get('completion_tokens_details') or {}
    entry['reasoning_tokens'] = details.get('reasoning_tokens') if isinstance(details, dict) else None
    with lock:
        usage_file.write(json.dumps(entry, ensure_ascii=False)+'\n')
        usage_file.flush()
    return normalized

def observed_run_suite(specs, context, **kwargs):
    global position
    item = schedule[position]
    if len(specs) != 1 or specs[0].task_id != item['task_id']:
        raise RuntimeError('Observer schedule mismatch')
    print(json.dumps(dict(event='started', execution=position+1, total=len(schedule), **item)), flush=True)
    outcomes, traces = original_run_suite(specs, context, **kwargs)
    if len(outcomes) != 1 or len(traces) != 1:
        raise RuntimeError('Observer expected exactly one trace and outcome')
    record = outcomes[0].model_dump()
    errors = ' '.join(paired.error_text(traces[0]))
    reason = paired._environment_error_reason(RuntimeError(errors)) if not record['passed'] else None
    contract_error = not record['passed'] and paired.provider_contract_error(errors)
    record.update(item, environment_error=reason is not None,
                  environment_error_reason=reason, provider_contract_error=contract_error,
                  raw_trace=traces[0])
    records.append(record)
    checkpoint.write(json.dumps(record, ensure_ascii=False)+'\n')
    checkpoint.flush()
    os.fsync(checkpoint.fileno())
    print(json.dumps(dict(event='completed', execution=position+1, total=len(schedule), **item,
                          passed=record['passed'], wall_ms=record.get('wall_ms'),
                          measured_tokens=record.get('measured_tokens'),
                          failure_class=record.get('failure_class'),
                          failed_checks=[check['name'] for check in record['checks'] if not check['passed']])), flush=True)
    position += 1
    if reason or contract_error:
        raise RuntimeError('Infrastructure/provider-contract failure; stop and preserve partial traces')
    if not record.get('measured_tokens') and 'model request failed' in errors.casefold():
        raise RuntimeError('Provider request failed without measured usage; stop and inspect')
    return outcomes, traces

BaseModelProvider.record_usage = observed_usage
paired.harness.run_suite = observed_run_suite
try:
    report = paired.run(args)
except Exception as exc:
    report = {'benchmark':'paired_agent_tasks', 'manifest':manifest, 'records':records,
              'executed': bool(records), 'summary':{'complete':False},
              'interruption':{'type':type(exc).__name__, 'reason':str(exc)},
              'execution_observer':observer}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'report':str(report_path),'complete':False,'executions':len(records),
                      'reason':str(exc)}), flush=True)
    raise SystemExit(1)
else:
    report['execution_observer'] = observer
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'report':str(report_path),'summary':report['summary']}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if report['summary']['complete'] else 1)
finally:
    checkpoint.close()
    usage_file.close()
