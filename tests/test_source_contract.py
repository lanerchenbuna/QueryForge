"""Source contracts reject coincidental values and preserve raw paid evidence."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from queryforge.evaluation import TaskSpec, TaskTrace, evaluate_task
from queryforge.evaluation.results import ResultContract
from scripts.audit_paired_benchmark import audit_report, digest_json
from scripts.freeze_live_tasks import frozen_tasks

ROOT = Path(__file__).resolve().parents[1]


def fixture_spec(**overrides):
    values = dict(task_id='source', split='dev', dataset='fixture', question='Receipt date keys',
                  expected_outcome='query', expected_values={'rows': [[1, 10]], 'columns': ['first_day', 'last_day']},
                  result_contract=ResultContract(required_columns=['first_day', 'last_day']),
                  answer_must_reference_evidence=False, expected_tables=['fact_order'],
                  reference_sql='SELECT MIN(date_key) AS first_day,MAX(date_key) AS last_day FROM fact_order')
    values.update(overrides)
    return TaskSpec(**values)


def fixture_trace(sql='SELECT MIN(date_key) AS first_day,MAX(date_key) AS last_day FROM dim_date'):
    return TaskTrace(task_id='source', payload={'status': 'success', 'sql': sql,
                    'rows': [[1, 10]], 'columns': ['first_day', 'last_day']},
                     usage={'total_tokens': 10, 'measured_tokens': 10}, provider='deepseek', model='deepseek-flash')


class SourceContractTest(unittest.TestCase):
    def test_bad_source_cannot_pass_matching_values(self):
        legacy = evaluate_task(fixture_spec(), fixture_trace())
        strict = evaluate_task(fixture_spec(required_sql_tables=['fact_order']), fixture_trace())
        self.assertTrue(legacy.passed)
        self.assertTrue(strict.check('expected_values').passed)
        self.assertFalse(strict.passed)
        self.assertEqual(strict.failure_class, 'unsupported_claim')
        self.assertEqual(strict.check('sql_sources').actual['missing_tables'], ['fact_order'])

    def test_correct_source_passes_and_missing_sql_is_unverified(self):
        spec = fixture_spec(required_sql_tables=['fact_order'])
        correct = fixture_trace(spec.reference_sql)
        self.assertTrue(evaluate_task(spec, correct).passed)
        correct.payload.pop('sql')
        check = evaluate_task(spec, correct).check('sql_sources')
        self.assertFalse(check.passed)
        self.assertFalse(check.actual['verified'])
        self.assertEqual(check.actual['reason'], 'missing_sql')

    def test_legacy_spec_has_no_source_check(self):
        self.assertIsNone(evaluate_task(fixture_spec(), fixture_trace()).check('sql_sources'))

    def test_malformed_source_constraints_cannot_silently_disable_check(self):
        for value in (None, 'fact_order', [''], [' '], [None], [3], ['fact_order', 'FACT_ORDER'], ['K'], ['main.fact_order']):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                fixture_spec(required_sql_tables=value)
        self.assertEqual(fixture_spec(required_sql_tables=[' FACT_ORDER ']).required_sql_tables, ['fact_order'])


class PairedAuditTest(unittest.TestCase):
    def fixture(self):
        spec = fixture_spec()
        trace = fixture_trace()
        result = evaluate_task(spec, trace).model_dump()
        records = [dict(result, repeat=r, arm=arm, raw_trace=trace.model_dump())
                   for r in range(2) for arm in ('baseline', 'treatment')]
        manifest = {'provider': 'deepseek', 'model': 'deepseek-flash', 'repeats': 2, 'seed': 17,
                    'task_count': 1, 'case_executions': 4,
                    'schedule': [{'task_id': 'source', 'repeat': r, 'order': ['baseline', 'treatment']} for r in range(2)],
                    'provenance': {'gold_sha256': digest_json([spec.model_dump(exclude={'required_sql_tables'})])}}
        report = {'manifest': manifest, 'records': records, 'summary': {'complete': True, 'provenance_stable': True},
                  'final_provenance': manifest['provenance']}
        usage = [dict(task_id='source', repeat=r, arm=arm, provider='deepseek', model='deepseek-flash',
                      at_utc='2026-10-11T09:00:00+00:00', prompt_tokens=6, completion_tokens=4, total_tokens=10,
                      prompt_cache_hit_tokens=2, prompt_cache_miss_tokens=4)
                 for r in range(2) for arm in ('baseline', 'treatment')]
        prices = {'version': 'test', 'currency': 'CNY', 'provider': 'deepseek', 'model': 'deepseek-flash',
                  'valid_from_utc': '2026-10-10T16:00:00+00:00', 'valid_until_utc': '2026-10-11T16:00:00+00:00',
                  'input_cache_hit_per_million': .02, 'input_cache_miss_per_million': 1, 'output_per_million': 4}
        return report, spec, usage, prices

    def audit(self, report, spec, usage, prices=None):
        return audit_report(report, [spec], [fixture_spec(required_sql_tables=['fact_order'])], usage, prices=prices)

    def test_source_rescoring_preserves_original_verdict_and_never_calls_a_model(self):
        report, spec, usage, prices = self.fixture()
        original = deepcopy(report)
        audited = self.audit(report, spec, usage, prices)
        self.assertEqual(report, original)
        self.assertEqual(audited['audit']['new_model_calls'], 0)
        self.assertEqual(len(audited['audit']['newly_rejected_cases']), 4)
        self.assertTrue(all(r['original_passed'] and not r['passed'] for r in audited['records']))
        self.assertEqual(audited['summary']['evaluable_pairs'], 2)
        self.assertAlmostEqual(audited['audit']['estimated_cost_cny'], 4 * (2 * .02 + 4 + 4 * 4) / 1_000_000)
        self.assertIsNone(audited['summary']['cost_usd']['delta'])

    def test_manifest_missing_entire_task_cannot_appear_complete(self):
        report, spec, usage, _ = self.fixture()
        report['records'] = []
        with self.assertRaisesRegex(ValueError, 'record count'):
            self.audit(report, spec, usage)

    def test_tampered_original_verdict_or_token_usage_is_rejected(self):
        for mutation, reason in (('verdict', 'verdict'), ('tokens', 'token mismatch')):
            report, spec, usage, _ = self.fixture()
            if mutation == 'verdict':
                report['records'][0]['passed'] = False
            else:
                usage[0]['total_tokens'] += 1
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, reason):
                self.audit(report, spec, usage)

    def test_question_or_reference_gold_changes_are_rejected(self):
        report, spec, usage, _ = self.fixture()
        changed = spec.model_copy(update={'question': 'Different question'})
        with self.assertRaisesRegex(ValueError, 'execution gold'):
            self.audit(report, changed, usage)
        scoring = fixture_spec(question='Different question', required_sql_tables=['fact_order'])
        with self.assertRaisesRegex(ValueError, 'model input'):
            audit_report(report, [spec], [scoring], usage)

    def test_unknown_cost_remains_unknown_and_wrong_price_window_is_rejected(self):
        report, spec, usage, prices = self.fixture()
        usage[0]['prompt_cache_hit_tokens'] = None
        audited = self.audit(report, spec, usage, prices)
        self.assertIsNone(audited['audit']['estimated_cost_cny'])
        self.assertIsNone(audited['records'][0]['cost_cny'])
        report, spec, usage, prices = self.fixture()
        prices['valid_until_utc'] = '2026-10-11T08:00:00+00:00'
        with self.assertRaisesRegex(ValueError, 'price window'):
            self.audit(report, spec, usage, prices)

    def test_invalid_price_cannot_be_reported_as_measured_cost(self):
        for invalid in (True, -1, float('inf'), None):
            report, spec, usage, prices = self.fixture()
            prices['output_per_million'] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'price rates'):
                self.audit(report, spec, usage, prices)

    def test_timeout_keeps_reported_usage_and_unknown_case_cost_separate(self):
        report, spec, usage, prices = self.fixture()
        trace = fixture_trace().model_copy(update={
            'usage': {'total_tokens': 15, 'estimated': True, 'model_calls': 2, 'measured_calls': 1},
            'model_spans': [
                {'kind': 'model', 'usage': {'prompt_tokens': 6, 'completion_tokens': 4,
                                          'total_tokens': 10, 'estimated': False}},
                {'kind': 'model', 'usage': {'prompt_tokens': 4, 'completion_tokens': 1,
                                          'total_tokens': 5, 'estimated': True}}]})
        outcome = evaluate_task(spec, trace).model_dump()
        report['records'][0] = dict(outcome, repeat=0, arm='baseline', raw_trace=trace.model_dump())
        original = deepcopy(report)
        audited = self.audit(report, spec, usage, prices)
        self.assertEqual(report, original)
        self.assertEqual(audited['audit']['token_mismatches'], [])
        self.assertEqual(audited['audit']['reported_response_tokens'], 40)
        self.assertEqual(audited['records'][0]['reported_response_tokens'], 10)
        self.assertEqual(audited['records'][0]['measured_tokens'], 0)
        self.assertIsNone(audited['records'][0]['cost_cny'])
        self.assertIsNone(audited['audit']['estimated_cost_cny'])
        self.assertGreater(audited['audit']['reported_response_cost_cny'], 0)
        self.assertEqual(audited['audit']['unknown_usage_cases'][0]['unknown_calls'], 1)
        usage[0]['completion_tokens'] += 1
        with self.assertRaisesRegex(ValueError, 'token mismatch'):
            self.audit(report, spec, usage, prices)

    def test_mixed_usage_without_complete_spans_fails_closed(self):
        report, spec, usage, _ = self.fixture()
        trace = fixture_trace().model_copy(update={'usage': {'total_tokens': 15, 'estimated': True,
                                                             'model_calls': 2, 'measured_calls': 1}})
        outcome = evaluate_task(spec, trace).model_dump()
        report['records'][0] = dict(outcome, repeat=0, arm='baseline', raw_trace=trace.model_dump())
        with self.assertRaisesRegex(ValueError, 'complete model spans'):
            self.audit(report, spec, usage)

    def test_sensitivity_exclusion_keeps_primary_results_and_all_raw_records(self):
        report, spec, usage, _ = self.fixture()
        other = fixture_spec(task_id='other', question='All stored order calendar keys')
        trace = fixture_trace(other.reference_sql).model_copy(update={'task_id': 'other'})
        outcome = evaluate_task(other, trace).model_dump()
        report['records'] += [dict(outcome, repeat=r, arm=arm, raw_trace=trace.model_dump())
                              for r in range(2) for arm in ('baseline', 'treatment')]
        report['manifest']['schedule'] += [{'task_id': 'other', 'repeat': r, 'order': ['baseline', 'treatment']}
                                          for r in range(2)]
        report['manifest'].update(case_executions=8, task_count=2)
        report['manifest']['provenance']['gold_sha256'] = digest_json(
            [s.model_dump(exclude={'required_sql_tables'}) for s in (spec, other)])
        usage += [dict(call, task_id='other') for call in list(usage)]
        scoring = [fixture_spec(required_sql_tables=['fact_order']),
                   other.model_copy(update={'required_sql_tables': ['fact_order']})]
        audited = audit_report(report, [spec, other], scoring, usage,
                               excluded_tasks={'source': 'Confirmed question/oracle ambiguity'})
        self.assertEqual(len(audited['records']), 8)
        self.assertEqual(audited['summary']['total_pairs'], 4)
        self.assertEqual(audited['summary']['accuracy']['baseline_mean'], .5)
        self.assertEqual(audited['sensitivity_summary']['total_pairs'], 2)
        self.assertEqual(audited['sensitivity_summary']['accuracy']['baseline_mean'], 1)
        self.assertEqual(audited['audit']['sensitivity_cost_cny']['pairs'], 0)
        with self.assertRaisesRegex(ValueError, 'known task ids'):
            audit_report(report, [spec, other], scoring, usage, excluded_tasks={'unknown': 'Bad gold'})
        with self.assertRaisesRegex(ValueError, 'explicit reasons'):
            audit_report(report, [spec, other], scoring, usage, excluded_tasks={'source': ''})


class LiveGoldContractTest(unittest.TestCase):
    def test_live_v1_remains_byte_identical_to_its_independent_oracles(self):
        frozen = frozen_tasks()
        for split, text in frozen.items():
            self.assertEqual((ROOT / 'evaluation/live_tasks' / f'{split}.jsonl').read_text(), text)

    def test_source_contract_preserves_model_input_and_reference_values(self):
        original, strict = frozen_tasks(), frozen_tasks(source_contract=True)
        for split in original:
            old = [json.loads(line) for line in original[split].splitlines()]
            new = [json.loads(line) for line in strict[split].splitlines()]
            self.assertEqual(len(old), len(new))
            for previous, current in zip(old, new):
                self.assertEqual(current['required_sql_tables'], previous['expected_tables'])
                for key in ('task_id', 'question', 'reference_sql', 'expected_values', 'result_contract'):
                    self.assertEqual(current[key], previous[key])

    def test_corrected_future_corpus_disambiguates_all_store_formats(self):
        source, corrected = frozen_tasks(source_contract=True), frozen_tasks(corrected_questions=True)
        changed = []
        for split in source:
            before = [json.loads(line) for line in source[split].splitlines()]
            after = [json.loads(line) for line in corrected[split].splitlines()]
            for previous, current in zip(before, after):
                if previous['question'] != current['question']:
                    changed.append(current['task_id'])
                    self.assertIn('regardless of its store_format', current['question'])
                    self.assertIn('Include all stores', current['question'])
                for key in ('reference_sql', 'expected_values', 'result_contract', 'required_sql_tables'):
                    self.assertEqual(current[key], previous[key])
        self.assertEqual(changed, ['live_retail_orders_regression_4'])


if __name__ == '__main__':
    unittest.main()
