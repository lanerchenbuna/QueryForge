"""Freeze explicit live-evaluation questions against unchanged control-flow SQL oracles."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

import sqlglot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from queryforge.evaluation import TaskSpec, load_spec_splits
from queryforge.evaluation.isolation import audit_splits, audit_dataset_schemas
from queryforge.evaluation.results import compare_results
from queryforge.evaluation.sources import check_sql_sources
from scripts.benchmark_agent import load_datasets

QUESTIONS = {
    'anime_streaming_dev_1': 'How many user accounts are stored? Return a single column named n.',
    'anime_streaming_dev_2': 'For each playback device type, show its total number of watch sessions over all stored dates. Return device_type and n, sorted by device_type.',
    'anime_streaming_dev_3': 'Count user accounts by country, including every country. Return country and n, sorted by country.',
    'anime_streaming_dev_4': 'What is the smallest stored episode identifier? Return one column named n.',
    'anime_streaming_dev_5': 'Return the identifier of the user whose user identifier is -1, if any. Return only user_id.',
    'anime_streaming_dev_6': 'What is the largest stored studio identifier? Return one column named n.',
    'support_tickets_holdout_1': 'How many support case records are stored? Return one column named n.',
    'support_tickets_holdout_2': 'Count all stored support cases by priority. Return priority and n, sorted by priority.',
    'support_tickets_holdout_3': 'For each support queue with at least one recorded case, show the queue name and number of cases. Return queue_name and n, sorted by queue_name.',
    'support_tickets_holdout_4': 'How many stored customer satisfaction survey responses have a NULL score? Return one column named n.',
    'support_tickets_holdout_5': 'List the identifiers of all recorded SLA breaches. Return only breach_id.',
    'support_tickets_holdout_6': 'How many support cases have been reopened? Count cases whose reopened flag is 1 and return one column named n.',
    'retail_orders_regression_1': 'How many order records are stored? Return one column named n.',
    'retail_orders_regression_2': 'Show the total net receipt amount in USD for each store region over all stored dates. Return region and amount, sorted by region.',
    'retail_orders_regression_3': 'Using a CTE, count all recorded receipt line items across all orders. Return one column named n.',
    'retail_orders_regression_4': 'For every outlet, return store_id and its position using dense rank by floor area, with the largest area ranked first. Include all outlets and sort the output by store_id.',
    'retail_orders_regression_5': 'Count receipt records with order dates from January 1 through March 31, 2024 inclusive. Return one column named n.',
    'retail_orders_regression_6': 'Count receipt records and receipt line records independently so that joining them does not inflate either count. Return orders and lines, in that order.',
    'retail_orders_regression_7': 'Show the three receipts with the largest net amounts. Return order_id and net_amount_usd, sorted by net_amount_usd descending, then order_id ascending for ties.',
    'retail_orders_regression_8': 'How many distinct payment methods occur in the stored receipts? Return one column named n.',
    'retail_orders_regression_9': 'Return the identifier of the store whose store identifier is -100, if any. Return only store_id.',
    'retail_orders_regression_10': 'Show the earliest and latest recorded receipt calendar keys. Return first_day and last_day in one row.',
    'retail_orders_regression_followup': '再查询一次刚才的订单数量，仍只返回 order_count 列。',
}


def frozen_tasks(*, source_contract=False, corrected_questions=False):
    source_contract = source_contract or corrected_questions
    datasets = load_datasets(ROOT / 'evaluation/datasets.json')
    specs = load_spec_splits(ROOT / 'evaluation/tasks')
    sources = {spec.task_id: spec for spec in specs if spec.runner == 'scripted_workflow'}
    if set(sources) != set(QUESTIONS):
        raise ValueError('Live question mapping must cover exactly the workflow fixtures')
    records = {split: [] for split in ('dev', 'regression', 'holdout')}
    live_specs = []
    for task_id, spec in sources.items():
        db = (ROOT / datasets[spec.dataset]['database']).resolve()
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            cursor = connection.execute(spec.reference_sql)
            rows = [list(row) for row in cursor.fetchall()]
            columns = [item[0] for item in cursor.description]
        if not compare_results(rows, columns, spec.expected_values['rows'], columns):
            raise ValueError(f'Source oracle drift: {task_id}')
        # Preserve live-v1 bytes: the source field was absent at execution time.
        record = spec.model_dump(mode='json', exclude_none=True, exclude={'required_sql_tables'})
        contract_version = 3 if corrected_questions else 2 if source_contract else 1
        record.update(task_id='live_' + task_id, template_id=f'live_v{contract_version}_' + spec.template_id,
                      question=QUESTIONS[task_id], expected_values={'rows': rows, 'columns': columns},
                      result_contract={'required_columns': columns, 'allow_extra_columns': False,
                                       'row_order': 'ordered' if sqlglot.parse_one(spec.reference_sql, read='sqlite').args.get('order') else 'multiset',
                                       'empty': 'allow' if not rows else 'forbid'},
                      notes='Live v1: explicit question/output contract; unchanged source reference SQL and data. Independent read-only SQLite oracle; original fixture retained.')
        if source_contract:
            record['required_sql_tables'] = list(spec.expected_tables)
            record['notes'] = 'Live v2: unchanged v1 question, reference SQL, data and output contract; adds independent physical source coverage for post-hoc trace auditing.'
            source_check = check_sql_sources(spec.reference_sql, spec.expected_tables)
            if not source_check.passed:
                raise ValueError(f'Reference source contract is unverified: {task_id}')
        if corrected_questions:
            record['notes'] = 'Live v3: retains reference SQL, data, output and source contracts; disambiguates store ranking across every store_format. No v3 model results claimed.'
            if task_id == 'retail_orders_regression_4':
                record['question'] = 'For every store, regardless of its store_format, return store_id and its position using dense rank by floor area, with the largest area ranked first. Include all stores and sort the output by store_id.'
        if getattr(spec, 'follow_up_context', None):
            record['follow_up_context'] = ['How many distinct orders are stored? Return one column named order_count.']
        live = TaskSpec.model_validate(record)
        live_specs.append(live)
        records[spec.split].append(record)
    audit_splits(live_specs)
    audit_dataset_schemas(live_specs, datasets, ROOT)
    return {split: ''.join(json.dumps(record, ensure_ascii=False, sort_keys=True) + '\n' for record in sorted(items, key=lambda r: r['task_id']))
            for split, items in records.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Check frozen files without writing')
    parser.add_argument('--source-contract', action='store_true', help='Freeze separate live-v2 source contracts; never changes live-v1')
    parser.add_argument('--correct-questions', action='store_true', help='Freeze separate live-v3 source contracts and corrected all-store ranking wording; no model calls')
    args = parser.parse_args()
    contract_version = 3 if args.correct_questions else 2 if args.source_contract else 1
    target = ROOT / ('evaluation/live_tasks_v3' if args.correct_questions else
                    'evaluation/live_tasks_v2' if args.source_contract else 'evaluation/live_tasks')
    frozen = frozen_tasks(source_contract=args.source_contract, corrected_questions=args.correct_questions)
    if args.check:
        for split, text in frozen.items():
            if (target / f'{split}.jsonl').read_text() != text:
                raise SystemExit(f'Frozen live task drift: {split}')
    else:
        target.mkdir(parents=True, exist_ok=True)
        for split, text in frozen.items():
            (target / f'{split}.jsonl').write_text(text)
    print(f'Live tasks v{contract_version}: 23 independent SQL oracles and explicit contracts verified')


if __name__ == '__main__':
    main()
