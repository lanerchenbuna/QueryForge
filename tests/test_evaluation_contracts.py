"""Mutation tests: incomplete answers cannot pass a complete task contract."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from queryforge.evaluation.results import ResultContract, compare_results
from queryforge.evaluation.evaluator import _check_expected_values
from queryforge.evaluation.tasks import TaskSpec
from queryforge.evaluation.isolation import audit_dataset_schemas
from tests.test_evaluate_sql import evaluate_sql
from scripts import benchmark_agent

ROOT = Path(__file__).resolve().parents[1]


class ResultContractTest(unittest.TestCase):
    def test_shared_column_cannot_hide_missing_metric(self):
        self.assertFalse(compare_results([[2]], ['count'], [[2, 60]], ['count', 'amount']))
        self.assertFalse(compare_results([[2, 9]], ['count', 'other'], [[2, 60]], ['count', 'amount']))

    def test_column_alignment_preserves_values_inside_rows(self):
        self.assertTrue(compare_results([[60, 2]], ['amount', 'count'], [[2, 60]], ['count', 'amount']))
        self.assertFalse(compare_results([[60, 2]], ['count', 'amount'], [[2, 60]], ['count', 'amount']))

    def test_only_declared_auxiliary_projection_is_tolerated(self):
        self.assertFalse(compare_results([[2, 60]], ['count', 'amount'], [[2]], ['count']))
        contract = ResultContract(required_columns=['count'], allow_extra_columns=True)
        self.assertTrue(compare_results([[2, 60]], ['count', 'amount'], [[2]], ['count'], contract=contract))
        self.assertTrue(compare_results([[2]], ['count'], [[2, 60]], ['count', 'helper'],
                                        contract=ResultContract(required_columns=['count'])))

    def test_order_is_explicit_and_duplicates_stay_significant(self):
        self.assertTrue(compare_results([[2], [1]], ['n'], [[1], [2]], ['n']))
        self.assertFalse(compare_results([[2], [1]], ['n'], [[1], [2]], ['n'], contract=ResultContract(row_order='ordered')))
        self.assertFalse(compare_results([[1], [2]], ['n'], [[1], [1]], ['n']))

    def test_empty_policy_and_shape_are_checked_even_without_values(self):
        self.assertTrue(compare_results([], ['n'], [], ['n']))
        self.assertFalse(compare_results([], None, [], ['n'], positional_aliases=True))
        self.assertFalse(compare_results([], ['n'], [], ['n'], contract=ResultContract(empty='forbid')))
        self.assertFalse(compare_results([], ['wrong'], [], ['n']))
        self.assertFalse(compare_results([[1, 2]], ['n'], [[1]], ['n']))
        self.assertFalse(compare_results([[1, 2]], ['n', 'n'], [[1, 2]], ['n', 'x']))

    def test_null_boolean_and_nonfinite_values_do_not_pass_as_numbers(self):
        for actual, expected in [(True, 1), (False, 0), (None, 0), (float('nan'), float('nan')), (float('inf'), float('inf'))]:
            self.assertFalse(compare_results([[actual]], ['n'], [[expected]], ['n']))
        self.assertTrue(compare_results([[None, .3]], ['n', 'v'], [[None, .1 + .2]], ['n', 'v']))

    def spec(self, **contract):
        return TaskSpec(task_id='mutation', split='dev', dataset='test', question='Both measures',
                        expected_outcome='query', expected_values={'rows': [[2, 60], [3, 70]], 'columns': ['count', 'amount']},
                        result_contract=ResultContract(**contract))

    def test_task_evaluator_honors_named_alignment_and_contract_order(self):
        payload = {'columns': ['amount', 'count'], 'rows': [[60, 2], [70, 3]]}
        self.assertTrue(_check_expected_values(self.spec(row_order='ordered'), payload).passed)
        payload['rows'].reverse()
        self.assertFalse(_check_expected_values(self.spec(row_order='ordered'), payload).passed)
        self.assertTrue(_check_expected_values(self.spec(), payload).passed)

    def test_task_evaluator_rejects_missing_metric_and_accepts_declared_extra(self):
        self.assertFalse(_check_expected_values(self.spec(), {'columns': ['count'], 'rows': [[2], [3]]}).passed)
        payload = {'columns': ['count', 'amount', 'helper'], 'rows': [[2, 60, 0], [3, 70, 1]]}
        self.assertFalse(_check_expected_values(self.spec(), payload).passed)
        self.assertTrue(_check_expected_values(self.spec(allow_extra_columns=True), payload).passed)

    def test_legacy_nested_rows_do_not_permute_cell_positions(self):
        spec = self.spec().model_copy(update={'result_contract': None})
        self.assertFalse(_check_expected_values(spec, {'columns': ['count', 'amount'], 'rows': [[60, 2], [70, 3]]}).passed)

    def test_zero_residual_tolerance_must_be_explicit_and_bounded(self):
        spec = self.spec().model_copy(update={'result_contract': None, 'expected_values': {'residual': 0}})
        self.assertFalse(_check_expected_values(spec, {'residual': 1e-12}).passed)
        spec.values_absolute_tolerance = {'residual': 1e-9}
        self.assertTrue(_check_expected_values(spec, {'residual': 1e-12}).passed)
        self.assertFalse(_check_expected_values(spec, {'residual': 1e-8}).passed)

    def test_nl2sql_end_to_end_requires_completion_and_reference_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'pairs.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('CREATE TABLE pairs (count INT, amount INT)')
                db.execute('INSERT INTO pairs VALUES (2,60), (3,70)')
            case = {'id': 'complete', 'domain': 'test', 'category': 'metric', 'expected_outcome': 'query',
                    'question': 'Both measures in order', 'database': str(database),
                    'expected_sql': 'SELECT count, amount FROM pairs ORDER BY count'}
            for status, columns, rows, correct in [
                ('success', ['count', 'amount'], [[2, 60], [3, 70]], True),
                ('partial', ['count', 'amount'], [[2, 60], [3, 70]], False),
                ('blocked', ['count', 'amount'], [[2, 60], [3, 70]], False),
                ('success', ['count'], [[2], [3]], False),
                ('success', ['count', 'amount'], [[3, 70], [2, 60]], False),
            ]:
                with self.subTest(status=status, columns=columns, rows=rows):
                    service = SimpleNamespace(ask=lambda *args: {'status': status, 'columns': columns, 'rows': rows, 'sql': case['expected_sql']})
                    report = evaluate_sql.evaluate_cases([case], service=service,
                        environment=evaluate_sql.EvaluationEnvironment(root / f'case{status}{len(columns)}{rows[0][0]}'),
                        default_database=str(database))
                    self.assertEqual(report['results'][0]['semantic_correct'], correct)


class SchemaIsolationTest(unittest.TestCase):
    def test_tool_trace_counts_each_governed_call_inside_a_query_step(self):
        spans = [{'kind': 'tool', 'duration_ms': 1, 'attributes': {'step_id': 'query_metric',
                  'action': 'query_metric', 'tool': name, 'tool_status': 'succeeded'}}
                 for name in ('check_data_quality', 'execute_sql', 'execute_sql')]
        payload = {'observability': {'spans': spans}, 'steps': [{'action': 'query_metric', 'tool_calls': 3}]}
        calls = benchmark_agent._tool_calls_from_payload(payload)
        self.assertEqual([c['tool'] for c in calls], ['check_data_quality', 'execute_sql', 'execute_sql'])
        self.assertTrue(all(c['ok'] for c in calls))

    def test_three_existing_schemas_have_independent_signatures(self):
        from queryforge.evaluation import load_spec_splits
        signatures = audit_dataset_schemas(load_spec_splits(ROOT / 'evaluation/tasks'), benchmark_agent.load_datasets(), ROOT)
        self.assertEqual(len(set(signatures.values())), 3)

    def test_renamed_and_copied_dataset_cannot_evade_physical_schema_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('train', 'test'):
                with sqlite3.connect(root / f'{name}.sqlite') as db:
                    db.execute('CREATE TABLE numbers (n INT PRIMARY KEY)')
                    db.execute('INSERT INTO numbers VALUES (?)', (1 if name == 'train' else 2,))
            datasets = {name: {'database': str(root / f'{name}.sqlite'), 'split_scope': split}
                        for name, split in [('train', 'dev'), ('test', 'holdout')]}
            specs = [SimpleNamespace(dataset=name, split=split) for name, split in [('train', 'dev'), ('test', 'holdout')]]
            with self.assertRaisesRegex(ValueError, 'physical schema split leakage'):
                audit_dataset_schemas(specs, datasets, root)
            specs[1].split = 'dev'
            with self.assertRaisesRegex(ValueError, 'split_scope mismatch'):
                audit_dataset_schemas(specs, datasets, root)
