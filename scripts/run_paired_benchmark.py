"""Balanced, repeated A/B experiments; tier 1 is control-flow QA, tier 3 costs API spend."""
from __future__ import annotations

import argparse
import json
import platform
import random
import sqlite3
import sys
import tempfile
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import benchmark_agent as harness
from scripts.evaluate_sql import _environment_error_reason
from queryforge.evaluation import load_spec_splits
from queryforge.evaluation.isolation import audit_splits, audit_corpus, audit_dataset_schemas
from queryforge.evaluation.paired import summarize_pairs


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tier', type=int, choices=(1, 3), default=1)
    parser.add_argument('--feature', choices=('candidates', 'skills'), default='candidates')
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--seed', type=int, default=17)
    parser.add_argument('--parallel-candidates', type=int, choices=(1, 2, 3), default=1,
                        help='Fixed count in a skills experiment; candidate experiment always compares 1 vs 2')
    parser.add_argument('--datasets', default=harness.DEFAULT_DATASETS)
    parser.add_argument('--tasks-dir', default=harness.DEFAULT_TASKS_DIR)
    parser.add_argument('--split', action='append', choices=('dev', 'regression', 'holdout'))
    parser.add_argument('--task', action='append', default=[])
    parser.add_argument('--retrieval-corpus', action='append', default=[])
    parser.add_argument('--provider')
    parser.add_argument('--model')
    parser.add_argument('--prices')
    parser.add_argument('--dry-run', action='store_true', help='Write the exact experiment manifest without calling a model')
    parser.add_argument('--report', required=True)
    return parser.parse_args(argv)


def error_text(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {'error', 'exception', 'error_message'} and isinstance(child, str):
                yield child
            else:
                yield from error_text(child)
    elif isinstance(value, list):
        for child in value:
            yield from error_text(child)


def provider_contract_error(text):
    text = text.casefold()
    return 'generate_with_messages' in text and any(marker in text for marker in
        ('unexpected keyword argument', 'required positional argument', 'positional arguments but'))


def run(args):
    if args.repeat < 2:
        raise ValueError('paired experiments require --repeat >= 2')
    if args.tier == 3 and not (args.provider and args.model):
        raise ValueError('tier 3 requires explicit --provider and --model')
    if args.tier == 1 and (args.provider or args.model):
        raise ValueError('tier 1 cannot use a real provider')
    datasets = harness.load_datasets(args.datasets)
    all_specs = load_spec_splits(harness._project_path(args.tasks_dir))
    audit_splits(all_specs)
    schema_hashes = audit_dataset_schemas(all_specs, datasets, ROOT)
    for corpus in args.retrieval_corpus:
        audit_corpus(all_specs, harness._project_path(corpus))
    selected = harness.select_specs(all_specs, splits=args.split or ['dev', 'regression', 'holdout'],
                                    task_ids=args.task, datasets=[], limit=0)
    specs = [s for s in selected if harness.spec_runner(s) in {'workflow', 'scripted_workflow'}]
    if not specs:
        raise ValueError('no workflow tasks selected for the paired experiment')
    if args.tier == 1 and any(harness.spec_runner(s) != 'scripted_workflow' for s in specs):
        raise ValueError('offline pairs require scripted_workflow tasks')
    prices = json.loads(harness._project_path(args.prices).read_text()) if args.prices else None
    if prices and not prices.get('version'):
        raise ValueError('price table must include a version')
    arms = ({'baseline': {'parallel_candidates': 1, 'skill_mode': 'auto'},
             'treatment': {'parallel_candidates': 2, 'skill_mode': 'auto'}} if args.feature == 'candidates' else
            {'baseline': {'parallel_candidates': args.parallel_candidates, 'skill_mode': 'off'},
             'treatment': {'parallel_candidates': args.parallel_candidates, 'skill_mode': 'auto'}})
    rng = random.Random(args.seed)
    starts = {s.task_id: rng.randrange(2) for s in specs}
    schedule = [{'task_id': s.task_id, 'repeat': repeat,
                 'order': ['baseline', 'treatment'] if (starts[s.task_id] + repeat) % 2 == 0 else ['treatment', 'baseline']}
                for repeat in range(args.repeat) for s in specs]
    manifest = {'tier': args.tier, 'mode': 'control_flow_only' if args.tier == 1 else 'model_e2e',
                'feature': args.feature, 'provider': args.provider, 'model': args.model, 'arms': arms,
                'fixed_controls': {'history_top_k': 0, 'vector_kb': False, 'today': '2024-12-31', 'fix_retries': 2},
                'task_count': len(specs), 'case_executions': len(schedule) * 2, 'repeats': args.repeat,
                'seed': args.seed, 'schema_sha256': schema_hashes, 'schedule': schedule,
                'excluded_tasks': [s.task_id for s in selected if s not in specs],
                'price_table': prices, 'api_spend': 'none' if args.tier == 1 or args.dry_run else 'requires human authorization under AGENTS.md'}
    with tempfile.TemporaryDirectory(prefix='qf_paired_') as state:
        context = harness.BenchmarkContext(datasets, Path(state), prices=prices, provider=args.provider, model=args.model)
        provenance = harness.build_report(tier=args.tier, specs=specs, outcomes=[], traces=[], context=context,
                                          thresholds={}, ablations=[], repeats=args.repeat)['provenance']
        manifest['provenance'] = provenance
        manifest['versions'] = {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                                'sqlglot': version('sqlglot'), 'pydantic': version('pydantic')}
        if args.dry_run:
            return {'benchmark': 'paired_manifest', 'manifest': manifest, 'executed': False}
        records = []
        by_id = {s.task_id: s for s in specs}
        for scheduled in schedule:
            for arm in scheduled['order']:
                arm_context = replace(context, **arms[arm])
                outcomes, traces = harness.run_suite([by_id[scheduled['task_id']]], arm_context, tier=args.tier)
                if len(outcomes) != 1:
                    raise ValueError('pair execution did not produce exactly one outcome')
                record = outcomes[0].model_dump()
                errors = ' '.join(error_text(traces[0]))
                reason = _environment_error_reason(RuntimeError(errors)) if not record['passed'] else None
                record.update(arm=arm, repeat=scheduled['repeat'], environment_error=reason is not None,
                              environment_error_reason=reason,
                              provider_contract_error=not record['passed'] and provider_contract_error(errors),
                              raw_trace=traces[0])
                records.append(record)
        final_provenance = harness.build_report(tier=args.tier, specs=specs, outcomes=[], traces=[], context=context,
                                                thresholds={}, ablations=[], repeats=args.repeat)['provenance']
        stable = all(provenance[key] == final_provenance[key] for key in
                     ('source_sha256', 'assets_sha256', 'datasets_sha256', 'contracts_sha256', 'gold_sha256'))
    summary = summarize_pairs(records, repeats=args.repeat, seed=args.seed)
    summary['provenance_stable'] = stable
    summary['complete'] = summary['complete'] and stable
    return {'benchmark': 'paired_agent_tasks', 'manifest': manifest, 'summary': summary, 'records': records,
            'executed': True, 'final_provenance': final_provenance,
            'offline_gate_passed': args.tier == 1 and all(r['passed'] for r in records)}


def main(argv=None):
    args = parse_args(argv)
    try:
        report = run(args)
        path = Path(args.report).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    except (ValueError, OSError) as exc:
        print(f'Paired benchmark refused: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'report': str(path), 'executed': report['executed'],
                      'tasks': report['manifest']['task_count'], 'case_executions': report['manifest']['case_executions'],
                      'summary': report.get('summary')}, ensure_ascii=False))
    if args.dry_run:
        return 0
    return 0 if report['summary']['complete'] and (args.tier == 3 or report['offline_gate_passed']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
