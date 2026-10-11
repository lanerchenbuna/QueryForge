"""Read-only replay of originally passing SQL through the sanctioned DatabaseTool."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from queryforge.domain.security.sql_policy import load_sql_policy
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.evaluation import load_spec_splits
from queryforge.evaluation.results import compare_results
from scripts.benchmark_agent import load_datasets

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('input_report',type=Path)
parser.add_argument('--report',type=Path,required=True)
args=parser.parse_args()
if args.report.exists():
    parser.error('output exists; preserve original audit')
original=json.loads(args.input_report.read_text())
if not original.get('summary',{}).get('complete'):
    parser.error('original experiment is incomplete')
datasets=load_datasets()
specs={s.task_id:s for s in load_spec_splits(ROOT/'evaluation/live_tasks')}
def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
before={name:digest(ROOT/info['database']) for name,info in datasets.items()}
if before != original['manifest']['provenance']['datasets_sha256']:
    parser.error('database bytes differ from the original experiment')
for name, info in datasets.items():
    expected=original['manifest']['provenance']['contracts_sha256'][name]['sql_policy']
    if digest(ROOT/info['sql_policy']) != expected:
        parser.error('SQL policy differs from the original experiment')
connectors={}
tools={}
records=[]
try:
    for record in original['records']:
        if not record['passed']:
            continue
        dataset=record['dataset']
        if dataset not in tools:
            connectors[dataset]=SQLiteConnector(str(ROOT/datasets[dataset]['database']))
            policy,path=load_sql_policy(ROOT/datasets[dataset]['sql_policy'])
            tools[dataset]=DatabaseTool(connectors[dataset],policy,policy_source_path=path)
        payload=record['raw_trace']['payload']
        spec=specs[record['task_id']]
        item={k:record[k] for k in ('task_id','repeat','arm')}
        try:
            result=tools[dataset].execute_sql(payload['sql'])
            item.update(trace_rows_match=compare_results(result.rows,result.columns,payload['rows'],payload['columns'],
                                                         contract=spec.result_contract),
                        reference_rows_match=compare_results(result.rows,result.columns,spec.expected_values['rows'],
                                                             spec.expected_values['columns'],contract=spec.result_contract))
        except Exception as exc:
            item.update(trace_rows_match=False,reference_rows_match=False,error_type=type(exc).__name__)
        records.append(item)
finally:
    for connector in connectors.values():
        connector.close()
after={name:digest(ROOT/info['database']) for name,info in datasets.items()}
audit={'input_report':str(args.input_report.resolve()),'input_report_sha256':digest(args.input_report),
       'script_sha256':digest(Path(__file__)),'replayed_executions':len(records),
       'trace_rows_matched':sum(r['trace_rows_match'] for r in records),
       'reference_rows_matched':sum(r['reference_rows_match'] for r in records),
       'databases_unchanged':before==after,'database_sha256':before,'new_model_calls':0,
       'note':'Supplementary engine replay through DatabaseTool and original SQL policy, not an independent policy/semantic verdict. The independent reference oracles were frozen separately. Matching rows still do not prove source intent.',
       'records':records}
args.report.parent.mkdir(parents=True,exist_ok=True)
with args.report.open('x') as output:
    json.dump(audit,output,ensure_ascii=False,indent=2);output.write('\n')
print(json.dumps({k:v for k,v in audit.items() if k!='records'}))
raise SystemExit(0 if audit['databases_unchanged'] and all(r['trace_rows_match'] and r['reference_rows_match'] for r in records) else 1)
