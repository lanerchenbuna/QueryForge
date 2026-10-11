"""Freeze phase-3 diagnostic gold from independent, explicit read-only SQL oracles."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.evaluate_sql import _execute_expected
from scripts.benchmark_agent import load_datasets


CASES = (
    ('anime_streaming', 'dev', 'watch_hours', 'fact_watch_session', 'watch_date_key', 'device_type',
     'SUM(watch_seconds)/3600.0', (202402, 202502),
     'Explain watch hours monthly for February 2025 year over year by device', 'complete'),
    ('retail_orders', 'regression', 'net_revenue', 'fact_order', 'order_date_key', 'sales_channel',
     'SUM(net_amount_usd)', (202402, 202403),
     'Explain net revenue monthly for March 2024 month over month by sales channel', 'unverified'),
    ('support_tickets', 'holdout', 'ticket_volume', 'fact_ticket', 'created_date_key', 'priority',
     'COUNT(DISTINCT ticket_id)', (202402, 202403),
     'Explain ticket volume monthly for March 2024 month over month by priority', 'unverified'),
)


def build_tasks():
    datasets, tasks = load_datasets(), {}
    for dataset, split, metric, table, time, category, expression, periods, question, completeness in CASES:
        scope = f'{time}/100 IN ({periods[0]}, {periods[1]})'
        sql = (f'SELECT printf(\'%04d-%02d\', {time}/10000, {time}/100%100) AS "calendar.month", '
               f'{category}, {expression} AS {metric} FROM {table} WHERE {scope} GROUP BY 1,2 ORDER BY 1,2')
        database = ROOT / datasets[dataset]['database']
        columns, rows = _execute_expected(database, sql)
        total_sql = f'SELECT {time}/100 AS period, {expression} AS value FROM {table} WHERE {scope} GROUP BY 1 ORDER BY 1'
        _, total_rows = _execute_expected(database, total_sql)
        totals = dict(total_rows)
        prefix = f'answer.diagnosis.contributions.{metric}'
        tasks[split] = dict(task_id=f'{dataset}_{split}_diagnostic', split=split, dataset=dataset,
            template_id=f'{dataset}_diagnostic_{category}', question=question, runner='planner',
            coverage=['business_diagnosis', 'time_category_grain', 'independent_reconciliation', 'required_projection'],
            expected_outcome='analysis', expected_status=['succeeded'],
            allowed_tools=['resolve_metric', 'check_data_quality', 'execute_sql', 'compare_periods', 'calculate_contribution', 'detect_anomaly'],
            required_steps=['resolve_metric', 'check_data_quality', 'query_metric', 'compare_periods', 'calculate_contribution', 'detect_anomaly', 'compose_answer'],
            required_evidence=['metric_resolution', 'data_quality', 'metric_value', 'metric_totals', 'period_comparison', 'contribution', 'anomaly'],
            expected_values={'answer.columns': columns, 'answer.rows': rows,
                             prefix+'.independent_baseline': totals[periods[0]], prefix+'.independent_current': totals[periods[1]],
                             prefix+'.total_delta': totals[periods[1]]-totals[periods[0]], prefix+'.residual': 0,
                             prefix+'.residual_current': 0, prefix+'.residual_baseline': 0, prefix+'.residual_explained': True,
                             'answer.diagnosis.interpretation': 'arithmetic_decomposition_not_causal_attribution',
                             'answer.diagnosis.period_completeness': completeness, 'final_answer.status': 'success'},
            result_contract={'required_columns': columns, 'columns_path': 'answer.columns', 'rows_path': 'answer.rows',
                             'row_order': 'ordered', 'empty': 'forbid'},
            values_absolute_tolerance={prefix+'.'+key: 1e-9 for key in ('residual', 'residual_current', 'residual_baseline')},
            answer_must_reference_evidence=True, reference_sql=sql, totals_reference_sql=total_sql,
            oracle_path='answer.rows', max_tool_calls=40,
            notes='Independent base-table SQL; no compiler or generated outputs are used to freeze expected values. Anomaly history has only two points and must be reported as insufficient_data.')
    return tasks


def main():
    directory = ROOT / 'evaluation/diagnostics'
    directory.mkdir(parents=True, exist_ok=True)
    for split, task in build_tasks().items():
        (directory / f'{split}.jsonl').write_text(json.dumps(task, ensure_ascii=False) + '\n')
    print('Frozen 3 diagnostic tasks on 3 physical schemas; model accuracy is not measured.')


if __name__ == '__main__':
    main()
