"""Recompute paired verdicts, source contracts and measured usage without model calls."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from queryforge.evaluation import TaskTrace, evaluate_task, load_spec_splits
from queryforge.evaluation.paired import summarize_pairs


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def case_key(record):
    return record['task_id'], record['repeat'], record['arm']


def reconcile_usage(calls, trace, record):
    """Reconcile reported responses; an estimated failed call stays unknown."""
    reported = sum(c['total_tokens'] for c in calls)
    if not (trace.usage or {}).get('estimated'):
        if reported != (record.get('measured_tokens') or 0):
            raise ValueError(f'provider/trace token mismatch: {case_key(record)}')
        return reported, 0
    spans = [s for s in trace.model_dump().get('model_spans', []) if s.get('kind') == 'model']
    if not spans or len(spans) != trace.usage.get('model_calls'):
        raise ValueError('mixed usage requires complete model spans')
    known, unknown = [], 0
    for span in spans:
        usage = span.get('usage')
        if not usage or usage.get('estimated'):
            unknown += 1
            continue
        fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        if any(type(usage.get(key)) is not int or usage[key] < 0 for key in fields):
            raise ValueError('mixed usage has malformed reported span')
        if usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
            raise ValueError('mixed usage reported span totals differ')
        known.append(tuple(usage[key] for key in fields))
    observed = Counter((c.get('prompt_tokens'), c.get('completion_tokens'), c['total_tokens']) for c in calls)
    if observed != Counter(known) or len(known) != trace.usage.get('measured_calls'):
        raise ValueError(f'provider/trace token mismatch: {case_key(record)}')
    return reported, unknown


def priced_cost(calls, trace, prices):
    if prices is None:
        return None
    if not calls:
        return 0.0 if trace.usage and trace.usage.get('model_calls') == 0 else None
    total = 0.0
    for call in calls:
        fields = ('prompt_tokens', 'completion_tokens', 'total_tokens',
                  'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')
        if any(type(call.get(key)) is not int or call[key] < 0 for key in fields):
            return None
        if (call['prompt_tokens'] != call['prompt_cache_hit_tokens'] + call['prompt_cache_miss_tokens']
                or call['total_tokens'] != call['prompt_tokens'] + call['completion_tokens']):
            return None
        if call.get('provider') != prices['provider'] or call.get('model') != prices['model']:
            raise ValueError('price provider/model mismatch')
        measured_at = datetime.fromisoformat(call['at_utc'])
        if not (datetime.fromisoformat(prices['valid_from_utc']) <= measured_at
                < datetime.fromisoformat(prices['valid_until_utc'])):
            raise ValueError('provider usage falls outside the frozen price window')
        total += (call['prompt_cache_hit_tokens'] * prices['input_cache_hit_per_million']
                  + call['prompt_cache_miss_tokens'] * prices['input_cache_miss_per_million']
                  + call['completion_tokens'] * prices['output_per_million']) / 1_000_000
    return total


def validate_prices(prices):
    if prices is None:
        return
    if not prices.get('version') or prices.get('currency') != 'CNY':
        raise ValueError('prices require a version and CNY currency')
    for key in ('input_cache_hit_per_million', 'input_cache_miss_per_million', 'output_per_million'):
        rate = prices.get(key)
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or rate < 0:
            raise ValueError('price rates must be finite non-negative numbers')
    start, end = (datetime.fromisoformat(prices[key]) for key in ('valid_from_utc', 'valid_until_utc'))
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('price window must be ordered and timezone-aware')


def audit_report(report, reference_specs, scoring_specs, usage, *, prices=None, excluded_tasks=None):
    """Preserve prespecified results and separately score an opt-in source contract."""
    validate_prices(prices)
    if not report.get('summary', {}).get('complete'):
        raise ValueError('audit requires a completed original experiment')
    manifest = report['manifest']
    if not report['summary'].get('provenance_stable'):
        raise ValueError('original execution provenance was not stable')
    schedule = manifest['schedule']
    expected = {(item['task_id'], item['repeat'], arm)
                for item in schedule for arm in item['order']}
    records = report['records']
    if len(expected) != manifest['case_executions'] or len(records) != len(expected):
        raise ValueError('manifest/record count mismatch')
    if len({case_key(r) for r in records}) != len(records) or {case_key(r) for r in records} != expected:
        raise ValueError('manifest cases missing or duplicated')
    task_ids = list(dict.fromkeys(item['task_id'] for item in schedule))
    excluded_tasks = dict(excluded_tasks or {})
    if not excluded_tasks.keys() <= set(task_ids) or any(not isinstance(reason, str) or not reason.strip()
                                                       for reason in excluded_tasks.values()):
        raise ValueError('sensitivity exclusions require known task ids and explicit reasons')
    if set(task_ids) <= excluded_tasks.keys():
        raise ValueError('sensitivity analysis excludes all tasks')
    reference = {s.task_id: s for s in reference_specs}
    scoring = {s.task_id: s for s in scoring_specs}
    if not set(task_ids) <= reference.keys() or not set(task_ids) <= scoring.keys():
        raise ValueError('missing reference or scoring task')
    gold = [reference[task_id].model_dump() for task_id in task_ids]
    # The source field did not exist in the preserved live-v1 execution model.
    legacy_gold = [{k: v for k, v in item.items() if k != 'required_sql_tables'} for item in gold]
    if manifest['provenance']['gold_sha256'] not in {digest_json(gold), digest_json(legacy_gold)}:
        raise ValueError('reference tasks differ from original execution gold')
    for task_id in task_ids:
        old, new = reference[task_id], scoring[task_id]
        permitted = {'required_sql_tables', 'template_id', 'notes'}
        if old.model_dump(exclude=permitted) != new.model_dump(exclude=permitted):
            raise ValueError(f'scoring changed model input or result contract: {task_id}')
        if not new.required_sql_tables:
            raise ValueError(f'scoring task has no declared source contract: {task_id}')
    by_case = defaultdict(list)
    for call in usage:
        key = case_key(call)
        if key not in expected:
            raise ValueError('provider response has no original case')
        if call.get('provider') != manifest['provider'] or call.get('model') != manifest['model']:
            raise ValueError('provider response does not match the experiment')
        by_case[key].append(call)
    audited, priced, source_failures, newly_rejected, unknown_usage_cases = [], [], [], [], []
    reported_costs = []
    for record in records:
        key = case_key(record)
        trace = TaskTrace.model_validate(record['raw_trace'])
        original = evaluate_task(reference[record['task_id']], trace, price_table=manifest.get('price_table'))
        if original.passed != record['passed'] or [c.model_dump() for c in original.checks] != record['checks']:
            raise ValueError(f'original verdict cannot be reproduced: {key}')
        calls = by_case[key]
        if any(type(c.get('total_tokens')) is not int or c['total_tokens'] < 0 for c in calls):
            raise ValueError(f'provider usage is not measurable: {key}')
        reported_tokens, unknown_calls = reconcile_usage(calls, trace, record)
        fresh = evaluate_task(scoring[record['task_id']], trace, price_table=manifest.get('price_table'))
        result = dict(record, **fresh.model_dump(), original_passed=record['passed'],
                      original_failure_class=record.get('failure_class'))
        reported_cost = priced_cost(calls, trace, prices)
        reported_costs.append(reported_cost)
        cost = None if unknown_calls else reported_cost
        result['cost_cny'] = cost
        result['reported_response_tokens'] = reported_tokens
        result['reported_response_cost_cny'] = reported_cost
        result['unknown_usage_calls'] = unknown_calls
        if unknown_calls:
            unknown_usage_cases.append({'case': list(key), 'reported_response_tokens': reported_tokens,
                                        'unknown_calls': unknown_calls,
                                        'note': 'Failed/estimated calls have no provider usage. Reported responses reconcile; total case cost remains unknown.'})
        audited.append(result)
        # summarize_pairs uses one generic cost slot; expose the result as CNY.
        priced.append(dict(result, cost_usd=cost, cost_basis='priced' if cost is not None else None))
        check = fresh.check('sql_sources')
        if check is not None and not check.passed:
            source_failures.append({'case': list(key), 'original_passed': record['passed'],
                                    'source_check': check.model_dump()})
        if record['passed'] and not fresh.passed:
            newly_rejected.append(list(key))
    summary = summarize_pairs(audited, repeats=manifest['repeats'], seed=manifest['seed'])
    summary['execution_provenance_stable'] = True
    sensitivity = ([r for r in audited if r['task_id'] not in excluded_tasks] if excluded_tasks else None)
    sensitivity_summary = (summarize_pairs(sensitivity, repeats=manifest['repeats'], seed=manifest['seed'])
                           if sensitivity is not None else None)
    return {'benchmark': 'post_hoc_source_contract_audit', 'manifest': manifest,
            'execution_final_provenance': report['final_provenance'],
            'execution_observer': report.get('execution_observer'),
            'original_summary': report['summary'], 'summary': summary, 'records': audited,
            'sensitivity_summary': sensitivity_summary,
            'audit': {'new_model_calls': 0, 'verdicts_recomputed': len(records),
                      'verdict_mismatches': [], 'token_mismatches': [],
                      'provider_responses': len(usage), 'price': prices,
                      'priced_executions': sum(r['cost_cny'] is not None for r in audited),
                      'estimated_cost_cny': sum(r['cost_cny'] for r in audited) if all(r['cost_cny'] is not None for r in audited) else None,
                      'reported_response_tokens': sum(r['reported_response_tokens'] for r in audited),
                      'reported_response_cost_cny': sum(reported_costs) if all(c is not None for c in reported_costs) else None,
                      'unknown_usage_cases': unknown_usage_cases,
                      'cost_cny': summarize_pairs(priced, repeats=manifest['repeats'], seed=manifest['seed'])['cost_usd'],
                      'sensitivity_cost_cny': (summarize_pairs([r for r in priced if r['task_id'] not in excluded_tasks],
                                                             repeats=manifest['repeats'], seed=manifest['seed'])['cost_usd']
                                               if excluded_tasks else None),
                      'source_failures': source_failures, 'newly_rejected_cases': newly_rejected},
            'scoring_policy': {'kind': 'post_hoc', 'required_sql_tables': True,
                               'sensitivity_excluded_tasks': excluded_tasks,
                               'note': 'Added after inspecting live-v1 traces. Necessary source coverage, not proof of full SQL semantics; original verdicts are preserved.'}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input_report', type=Path)
    parser.add_argument('--reference-tasks-dir', type=Path, required=True)
    parser.add_argument('--tasks-dir', type=Path, required=True)
    parser.add_argument('--usage', type=Path, required=True)
    parser.add_argument('--prices', type=Path)
    parser.add_argument('--exclude-task', action='append', default=[], help='Separate post-hoc sensitivity analysis; primary results retain every task')
    parser.add_argument('--exclusion-reason', help='Required rationale for sensitivity exclusions')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.exclude_task and not args.exclusion_reason:
        parser.error('--exclude-task requires --exclusion-reason')
    if args.report.exists():
        parser.error('output report already exists; original evidence must not be overwritten')
    try:
        original = json.loads(args.input_report.read_text())
        usage = [json.loads(line) for line in args.usage.read_text().splitlines() if line.strip()]
        prices = json.loads(args.prices.read_text()) if args.prices else None
        report = audit_report(original, load_spec_splits(args.reference_tasks_dir),
                              load_spec_splits(args.tasks_dir), usage, prices=prices,
                              excluded_tasks={task: args.exclusion_reason for task in args.exclude_task})
        def digest(path):
            return hashlib.sha256(path.read_bytes()).hexdigest()
        evaluator_files = sorted((ROOT / 'queryforge/evaluation').glob('*.py'))
        report['scoring_provenance'] = {
            'input_report': str(args.input_report.resolve()),
            'input_report_sha256': digest(args.input_report), 'usage_sha256': digest(args.usage),
            'price_sha256': digest(args.prices) if args.prices else None,
            'script_sha256': digest(Path(__file__)),
            'evaluator_sha256': digest_json({p.name: digest(p) for p in evaluator_files}),
            'gold_sha256': digest_json([s.model_dump() for s in load_spec_splits(args.tasks_dir)]),
            'tasks_dir': str(args.tasks_dir.resolve()),
            'versions': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                         'sqlglot': version('sqlglot'), 'pydantic': version('pydantic')}}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open('x') as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write('\n')
        print(json.dumps({'report': str(args.report), 'executions': len(report['records']),
                          'summary': report['summary'], 'newly_rejected_cases': len(report['audit']['newly_rejected_cases']),
                          'sensitivity_summary': report['sensitivity_summary'],
                          'new_model_calls': 0}, ensure_ascii=False))
        return 0
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(f'audit failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
