"""Scenario library for the API/repair/growth demos (step 17, Demo E).

These scenarios drive the *real* HTTP handlers, the real workflow repair loop and a
governed diagnostic template, all offline with a scripted model for repair. `docs/demo/run_demo_e.py`
narrates and asserts them; this module holds the scenario code so the demo entry
point stays readable.

Originally `scripts/demo_data_agent.py`; folded into `docs/demo/` so step 17 has a
single documented demo surface (the duplicate was flagged in review).

This uses scripted SQL generation for repair and deterministic governed diagnostics.
It makes no claim about live-model accuracy or autonomous root-cause inference.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from queryforge.application import AgentService, AgentOptions
from queryforge.application.analysis_planner import AnalysisPlannerService
from queryforge.core.config import Config
from queryforge.core.schemas.models import SqlTask
from queryforge.domain.domains import DomainResolver
from queryforge.workflow.workflow_runner import WorkflowRunner
from scripts.benchmark_runners import ScriptedModel  # repo script, kept for the scripted model


def config_at(root: Path) -> Config:
    return Config('scripted', None, 'demo-fixture', None, str(root/'unused.sqlite'),
                  history_db_path=str(root/'history.db'), orchestration_state_root=str(root/'runs'),
                  domain_registry_path=str(root/'registry.json'), allowed_database_paths=(str(root),),
                  api_key='local-demo-test-key')


def demo_upload(root: Path) -> dict:
    from fastapi.testclient import TestClient
    from queryforge.interfaces.api.app import create_app
    config=config_at(root)
    service=AgentService(config_loader=lambda **_: config,
                         llm_factory=lambda _: ScriptedModel('SELECT COUNT(items.id) AS item_count FROM items'))
    contract=dict(entity='items',description='Demo catalog',owner='demo',reviewed_by='demo',
                  sensitivity='internal',grain=['id'],primaryKey=['id'],
                  dimensions=[dict(name='id',column='id'),dict(name='name',column='name')],
                  metrics=[dict(name='item_count',description='Catalog size',aggregation='count',expression='COUNT(items.id)')])
    headers={'x-api-key':config.api_key}
    with TestClient(create_app(service)) as client:
        def upload(csv):
            return client.post('/domains/demo/publish',headers=headers,data={'contract':json.dumps(contract)},
                               files={'files':('items.csv',csv,'text/csv')})
        first=upload('id,name\n1,alpha\n2,beta\n');assert first.status_code==200,first.text
        version=first.json()['domain']['data_version']
        bad=upload('id,name\n1,alpha\n1,duplicate\n');assert bad.status_code==400,bad.text
        assert DomainResolver.from_config(config).resolve('demo').data_version==version
        final=upload('id,name\n1,alpha\n2,beta\n3,gamma\n');assert final.status_code==200,final.text
        answer=client.post('/ask',headers=headers,json=dict(question='item_count',domain_id='demo',history_top_k=0,complexity_mode='simple'))
        assert answer.status_code==200,answer.text
        assert answer.json()['rows']==[[3]],answer.text
        # Unauthenticated requests and path overrides have actual negative oracles.
        assert client.post('/ask',json={'question':'item_count','domain_id':'demo'}).status_code==401
        denied=client.post('/analyze',headers=headers,json={'question':'item_count','database':'/etc/passwd'})
        assert denied.status_code==400,denied.text
    return dict(mode='scripted_model_real_api_sql',old_version_preserved=True,quality_rejection=bad.json(),
                uploaded_count=3,rows=answer.json()['rows'],published_version=final.json()['domain']['data_version'])


def demo_repair(root: Path) -> dict:
    database=root/'sales.sqlite'
    with sqlite3.connect(database) as c:
        c.execute('CREATE TABLE sales(id INTEGER PRIMARY KEY, amount REAL, valid INTEGER)')
        c.executemany('INSERT INTO sales VALUES (?,?,?)',[(1,10,1),(2,20,1),(3,99,0)])
        assert c.execute('SELECT SUM(amount) FROM sales WHERE valid=0').fetchone()[0]==99
    semantic=dict(version=1,name='sales',entities=[dict(name='sale',table='sales',entity_type='fact',expected_columns=['id','amount','valid'],primary_key=['id'],grain=['id'])],
                  metrics=[dict(name='net_sales',description='Valid revenue',entity='sale',aggregation='sum',expression='SUM(sales.amount)',
                                synonyms=['net sales'],default_filters=['sales.valid = 1'])])
    path=root/'sales.yml';path.write_text(yaml.safe_dump(semantic))
    config=replace(config_at(root),database_path=str(database),semantic_model_path=str(path))
    class RepairModel(ScriptedModel):
        def generate_json(self,prompt):
            if 'Repair the' in prompt:
                return {'fixed_sql':'SELECT SUM(sales.amount) AS net_sales FROM sales WHERE sales.valid = 1',
                        'explanation':'Apply the governed valid-sales definition', 'tables_used':['sales']}
            return super().generate_json(prompt)
    payload=WorkflowRunner(config,llm_factory=lambda _: RepairModel('SELECT SUM(sales.amount) AS net_sales FROM sales WHERE sales.valid = 0'),
                           semantic_model_path=str(path),history_top_k=0,show_run_summary=True).run(
        SqlTask(question='net sales',database_path=str(database)))
    assert payload['rows']==[[30.0]],payload
    assert any(n['name']=='fix' for n in payload['run_summary']['workflow_nodes']),payload
    return dict(mode='scripted_model_real_semantic_repair',wrong_executable_value=99,gold_value=30,trace=payload)


def demo_analysis(root: Path, *, paid_last: int = 20) -> dict:
    """Three months, two channels; independent totals reconcile the computed drivers."""
    database=root/'growth.sqlite'
    with sqlite3.connect(database) as c:
        c.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, month TEXT, channel TEXT)')
        rows=[]
        for month,paid in [('2024-01',60),('2024-02',40),('2024-03',paid_last)]:
            for channel,n in [('paid',paid),('organic',40)]:
                start=len(rows)
                rows.extend((start+i+1,month+'-01',channel) for i in range(n))
        c.executemany('INSERT INTO users VALUES (?,?,?)',rows)
    semantic=dict(version=1,name='growth',entities=[dict(name='user',table='users',entity_type='fact',expected_columns=['id','month','channel'],primary_key=['id'],grain=['id'],
                   dimensions=[dict(name='month',column='month'),dict(name='channel',column='channel')])],
                   metrics=[dict(name='new_users',description='New users registered in each month',entity='user',aggregation='count',expression='COUNT(users.id)',synonyms=['new users'],
                                 allowed_dimensions=['user.month','user.channel'], time_field='users.month',
                                 analysis={'unit':'users','additivity':'additive','time':{'role':'registration',
                                     'coverage_start':'2024-01-01','complete_through':'2024-03-31','require_complete_periods':True}})])
    path=root/'growth.yml';path.write_text(yaml.safe_dump(semantic))
    config=replace(config_at(root),database_path=str(database),semantic_model_path=str(path))
    planner=AnalysisPlannerService(config_loader=lambda:config)
    clarification=planner.analyze('分析用户增长下降的原因')
    assert clarification['status']=='needs_clarification',clarification
    payload=planner.analyze('Explain new users monthly for March 2024 month over month by channel',run_id='growth-demo')
    assert payload['status']=='succeeded',payload
    contribution=next(e['payload'] for e in payload['evidence'] if e['kind']=='contribution')
    return dict(mode='governed_diagnostic_independent_totals',clarification=clarification,
                expected_delta=paid_last-40,contribution=contribution,trace=payload)
