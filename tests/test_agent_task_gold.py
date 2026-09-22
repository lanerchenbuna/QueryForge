"""Independent SQL oracles, split contamination, repeats and score reconstruction."""
import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.evaluation import load_spec_splits, recompute
from queryforge.evaluation.isolation import audit_splits, audit_corpus
from scripts import benchmark_agent as harness

ROOT=Path(__file__).resolve().parents[1]


class TaskGoldTest(unittest.TestCase):
    def setUp(self):
        self.specs=load_spec_splits(ROOT/'evaluation/tasks')
        self.datasets=harness.load_datasets()

    def test_three_physical_schemas_splits_and_reference_values(self):
        audit_splits(self.specs)
        self.assertEqual(len(self.datasets),3)
        self.assertEqual({s.split for s in self.specs},{'dev','regression','holdout'})
        schemas=[]
        for ds in self.datasets.values():
            with sqlite3.connect(ROOT/ds['database']) as c:
                schemas.append(tuple(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")))
        self.assertEqual(len(set(schemas)),3)
        for s in self.specs:
            if s.expected_outcome not in {'query','analysis'} or not getattr(s,'reference_sql',None): continue
            with sqlite3.connect(ROOT/self.datasets[s.dataset]['database']) as c:
                rows=[list(r) for r in c.execute(s.reference_sql)]
            key=getattr(s,'oracle_path','rows')
            actual=rows[0][0] if key.endswith('.value') else rows
            self.assertEqual(s.expected_values[key],actual,s.task_id)

    def test_holdout_question_or_sql_in_index_fails(self):
        s=next(s for s in self.specs if s.split=='holdout')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'index.jsonl'
            for field,text in [('question',s.question),('sql',s.reference_sql)]:
                p.write_text(json.dumps({'metadata':{field:text}})+'\n')
                with self.assertRaisesRegex(ValueError,'contamination'): audit_corpus(self.specs,p)
        changed=s.model_copy(update={'split':'dev','task_id':'leak'})
        with self.assertRaisesRegex(ValueError,'split leakage'): audit_splits([*self.specs,changed])

    def test_repeat_is_executed_and_recompute_ignores_stored_verdict(self):
        selected=[s for s in self.specs if s.expected_outcome=='clarification'][:1]
        with tempfile.TemporaryDirectory() as d:
            context=harness.BenchmarkContext(self.datasets,Path(d),repeats=2)
            outcomes,traces=harness.run_suite(selected,context)
            report=harness.build_report(tier=1,specs=selected,outcomes=outcomes,traces=traces,
                                       context=context,thresholds={},ablations=[],repeats=2)
        self.assertEqual(len(traces),2)
        self.assertEqual([t['repeat'] for t in traces],[0,1])
        report['results'][0]['passed']=False
        self.assertEqual(recompute(report,selected)['task_success_rate'],1)

    def test_evaluator_does_not_import_runtime_being_graded(self):
        # The evaluator must not import the machinery it grades, or a change to
        # the runtime could silently change what "passing" means. Check plain
        # `import` as well as `from ... import`, and recurse into subpackages:
        # ast.walk already descends into function/class bodies.
        forbidden=('queryforge.application','queryforge.workflow','queryforge.orchestration','queryforge.interfaces')
        checked=0
        for p in sorted((ROOT/'queryforge/evaluation').rglob('*.py')):
            checked+=1
            for node in ast.walk(ast.parse(p.read_text())):
                if isinstance(node,ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(forbidden),f'{p}:{node.lineno}')
                elif isinstance(node,ast.Import):
                    for a in node.names:
                        self.assertFalse(a.name.startswith(forbidden),f'{p}:{node.lineno}')
        self.assertGreater(checked,0,'the evaluator package was not found')

    def test_live_dispatch_does_not_use_planner_or_reference_sql(self):
        spec=next(s for s in self.specs if s.expected_outcome=='query')
        with tempfile.TemporaryDirectory() as d, patch('scripts.benchmark_runners.run_workflow_task') as run:
            run.return_value={'task_id':spec.task_id,'payload':{},'error':'rate limited','wall_ms':1}
            outcomes,_=harness.run_suite([spec],harness.BenchmarkContext(self.datasets,Path(d),provider='test',model='test'),tier=3)
            run.assert_called_once()
            self.assertFalse(outcomes[0].passed)

    def test_uncertain_usage_not_priced_as_actual_bill(self):
        self.assertIsNone(harness._cost_usd({'prompt_tokens':100,'completion_tokens':20,'estimated':True},
                                          {'input_per_million':1,'output_per_million':2}))
