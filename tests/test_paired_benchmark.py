"""Pair completeness, environment denominators, uncertainty and isolated execution."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from datetime import date
from unittest.mock import patch

from queryforge.evaluation.paired import summarize_pairs
from scripts import run_paired_benchmark as paired
from scripts import benchmark_agent as harness
from scripts import benchmark_runners
from queryforge.evaluation import load_spec_splits

ROOT = Path(__file__).resolve().parents[1]


def observations(repeats=2):
    return [dict(task_id=task, dataset='schema' + task, repeat=repeat, arm=arm,
                 passed=(arm == 'treatment' if task == 'a' else arm == 'baseline'), wall_ms=10,
                 measured_tokens=None, cost_usd=None, cost_basis=None)
            for task in ('a', 'b') for repeat in range(repeats) for arm in ('baseline', 'treatment')]


class PairedBenchmarkTest(unittest.TestCase):
    def test_repeats_remain_in_task_clusters(self):
        short = summarize_pairs(observations(2), repeats=2)
        long = summarize_pairs(observations(10), repeats=10)
        self.assertEqual(short['accuracy']['delta'], 0)
        self.assertEqual(short['accuracy']['ci95'], [-1, 1])
        self.assertEqual(short['accuracy']['ci95'], long['accuracy']['ci95'])
        self.assertEqual(long['accuracy']['unique_tasks'], 2)
        self.assertEqual(long['total_pairs'], 20)

    def test_environment_failure_excludes_the_entire_pair(self):
        records = observations()
        records[1]['environment_error'] = True
        result = summarize_pairs(records, repeats=2)
        self.assertEqual((result['total_pairs'], result['evaluable_pairs']), (4, 3))
        self.assertEqual(result['excluded_environment_pairs'], [{'task_id': 'a', 'repeat': 0, 'arms': ['treatment']}])
        self.assertFalse(result['complete'])

    def test_provider_signature_failure_is_not_model_accuracy(self):
        self.assertTrue(paired.provider_contract_error("TypeError: generate_with_messages() got an unexpected keyword argument 'tools'"))
        self.assertFalse(paired.provider_contract_error("SQL syntax error near generate_with_messages"))
        records = observations()
        records[1]['provider_contract_error'] = True
        result = summarize_pairs(records, repeats=2)
        self.assertEqual(result['evaluable_pairs'], 3)
        self.assertEqual(len(result['excluded_provider_contract_pairs']), 1)
        self.assertFalse(result['complete'])

    def test_unknown_usage_and_cost_stay_unknown(self):
        records = observations()
        for r in records:
            r.update(estimated_tokens=999, cost_usd=1, cost_basis='unknown')
        result = summarize_pairs(records, repeats=2)
        self.assertEqual(result['measured_tokens']['pairs'], 0)
        self.assertIsNone(result['cost_usd']['delta'])
        for r in records:
            r.update(measured_tokens=100 if r['arm'] == 'baseline' else 150, cost_basis='priced')
        result = summarize_pairs(records, repeats=2)
        self.assertEqual(result['measured_tokens']['delta'], 50)
        self.assertEqual(result['cost_usd']['delta'], 0)

    def test_missing_duplicate_or_mismatched_pairs_fail_closed(self):
        for records, pattern in [(observations()[:-1], 'missing pair'),
                                 ([*observations(), observations()[0]], 'duplicate pair arm')]:
            with self.assertRaisesRegex(ValueError, pattern):
                summarize_pairs(records, repeats=2)
        records = observations()
        records[1]['dataset'] = 'different'
        with self.assertRaisesRegex(ValueError, 'dataset mismatch'):
            summarize_pairs(records, repeats=2)
        with self.assertRaisesRegex(ValueError, 'at least two'):
            summarize_pairs(observations(), repeats=1)

    def args(self, directory, *extra):
        return paired.parse_args(['--report', str(Path(directory) / 'paired.json'), '--repeat', '2',
                                  '--task', 'anime_streaming_dev_1', '--task', 'support_tickets_holdout_1', *extra])

    def test_dry_run_fixes_inputs_controls_versions_and_balanced_order(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'run_suite', side_effect=AssertionError('dry run called runtime')):
            report = paired.run(self.args(directory, '--dry-run'))
        self.assertFalse(report['executed'])
        manifest = report['manifest']
        self.assertEqual(manifest['arms']['baseline'], {'parallel_candidates': 1, 'skill_mode': 'auto'})
        self.assertEqual(manifest['arms']['treatment'], {'parallel_candidates': 2, 'skill_mode': 'auto'})
        for task in {s['task_id'] for s in manifest['schedule']}:
            orders = [s['order'] for s in manifest['schedule'] if s['task_id'] == task]
            self.assertEqual(orders[0], list(reversed(orders[1])))
        self.assertEqual(len(set(manifest['schema_sha256'].values())), 3)
        self.assertIn('contracts_sha256', manifest['provenance'])
        self.assertIn('sqlglot', manifest['versions'])

    def test_offline_repeated_pairs_close_the_execution_and_evaluation_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            report = paired.run(self.args(directory))
        self.assertTrue(report['offline_gate_passed'], [(r['task_id'], r['checks']) for r in report['records'] if not r['passed']])
        self.assertEqual(report['summary']['total_pairs'], 4)
        self.assertEqual(len(report['records']), 8)
        self.assertEqual(report['summary']['accuracy']['delta'], 0)
        self.assertIsNone(report['summary']['measured_tokens']['delta'])
        roots = [r['raw_trace']['payload'].get('run_id') for r in report['records']]
        self.assertEqual(len(set(roots)), len(roots))
        self.assertEqual(report['manifest']['mode'], 'control_flow_only')

    def test_live_manifest_can_be_reviewed_without_api_spend(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(harness, 'run_suite', side_effect=AssertionError('model called')):
            report = paired.run(self.args(directory, '--tier', '3', '--provider', 'deepseek', '--model', 'deepseek-chat', '--dry-run', '--feature', 'skills'))
        self.assertEqual(report['manifest']['arms'], {'baseline': {'parallel_candidates': 1, 'skill_mode': 'off'},
                                                     'treatment': {'parallel_candidates': 1, 'skill_mode': 'auto'}})
        self.assertEqual(report['manifest']['api_spend'], 'none')

    def test_cli_rejects_offline_provider_and_single_repeat(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()):
            for extra in (['--provider', 'test'], ['--repeat', '1']):
                self.assertEqual(paired.main(['--report', str(Path(directory) / 'fail.json'), *extra]), 2)

    def test_workflow_controls_reach_runner_including_followup_sessions(self):
        specs = load_spec_splits(ROOT / 'evaluation/tasks')
        spec = next(s for s in specs if s.expected_outcome == 'query' and not getattr(s, 'follow_up_context', None))
        datasets = harness.load_datasets()
        ds = {k: str(ROOT / v) if k in {'database', 'semantic_model', 'sql_policy'} else v for k, v in datasets[spec.dataset].items()}
        with tempfile.TemporaryDirectory() as directory, patch.object(benchmark_runners, 'WorkflowRunner') as runner:
            runner.return_value.run.return_value = {'status': 'success'}
            context = harness.BenchmarkContext(datasets, Path(directory), parallel_candidates=3, skill_mode='off')
            benchmark_runners.run_workflow_task(spec, ds, context)
            kwargs = runner.call_args.kwargs
            self.assertEqual((kwargs['parallel_candidates'], kwargs['selected_skills']), (3, []))
        followup = spec.model_copy(update={'follow_up_context': ['warmup']})
        with tempfile.TemporaryDirectory() as directory, patch('queryforge.application.AgentService') as service:
            service.return_value.ask.return_value = {'status': 'success'}
            context = harness.BenchmarkContext(datasets, Path(directory), parallel_candidates=2, skill_mode='auto')
            benchmark_runners.run_workflow_task(followup, ds, context)
            options = service.return_value.ask.call_args.args[1]
            self.assertEqual(options.parallel_candidates, 2)
            self.assertIsNone(options.skills)
            self.assertFalse(options.enable_vector_kb)
            self.assertEqual(options.vector_top_k, 0)
            factory = service.call_args.kwargs['runner_factory']
            self.assertEqual(factory.keywords['today_provider'](), date(2024, 12, 31))

    def test_followup_replay_uses_frozen_date_in_every_turn(self):
        spec = next(s for s in load_spec_splits(ROOT / 'evaluation/tasks') if getattr(s, 'follow_up_context', None))
        datasets = harness.load_datasets()
        ds = {k: str(ROOT / v) if k in {'database', 'semantic_model', 'sql_policy'} else v
              for k, v in datasets[spec.dataset].items()}
        with tempfile.TemporaryDirectory() as directory:
            context = harness.BenchmarkContext(datasets, Path(directory), parallel_candidates=1, skill_mode='off')
            trace = benchmark_runners.run_workflow_task(spec, ds, context)
        self.assertIsNone(trace['error'])
        payload = trace['payload']
        for turn in [*payload['previous_turns'], payload]:
            self.assertEqual(turn['date_context']['reference_date'], '2024-12-31')
