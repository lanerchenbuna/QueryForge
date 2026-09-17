"""Exercise actual optional SDKs; tier 2 treats any skip as a failed gate."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class OptionalIntegrationsTest(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('lancedb'),'vector extra required')
    def test_actual_lancedb_scope_filter_precedes_top_k(self):
        from queryforge.infrastructure.storage.vector_store import LanceDBVectorStore,VectorDocument
        class Embeddings:
            def embed(self,texts): return [[1.,float('other' in t),0.] for t in texts]
        with tempfile.TemporaryDirectory() as d:
            store=LanceDBVectorStore(d,embedding_provider=Embeddings())
            docs=[VectorDocument.create(id=key,text=text,source_type='schema',metadata={'domain_id':domain,'data_version':'1'})
                  for key,text,domain in [('a','nearest private','private'),('b','other allowed','allowed')]]
            self.assertEqual(store.add_documents(docs),2)
            results=store.search('nearest',top_k=1,filters={'domain_id':'allowed','data_version':'1'})
            self.assertEqual([r.id for r in results],['b'])
            self.assertEqual(store.search('nearest',filters={'domain_id':'missing'}),[])
            self.assertEqual(store.upsert_documents(docs)['unchanged'],2)

    @unittest.skipUnless(importlib.util.find_spec('mcp'),'MCP extra required')
    def test_real_mcp_sdk_and_cli_match_real_service_results(self):
        from dataclasses import replace
        from io import StringIO
        from contextlib import redirect_stdout
        from unittest.mock import patch
        import sqlite3
        import yaml
        import sys

        sys.path.insert(0, str(Path('docs/demo')))
        from scenarios_api_and_repair import config_at
        from scripts.benchmark_runners import ScriptedModel
        from queryforge.application import AgentService,AgentOptions
        from queryforge.interfaces.mcp.server import create_mcp_server
        from queryforge import cli
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'items.sqlite'
            with sqlite3.connect(p) as c:
                c.execute('CREATE TABLE items(id INTEGER PRIMARY KEY)');c.executemany('INSERT INTO items VALUES (?)',[(1,),(2,)])
            config=replace(config_at(root),database_path=str(p),require_semantic_model=False)
            service=AgentService(config_loader=lambda **_:config,llm_factory=lambda _:ScriptedModel('SELECT id FROM items ORDER BY id'))
            expected=service.ask('List item IDs',AgentOptions(database=str(p),allow_schema_only=True,skills=[],history_top_k=0))['rows']
            mcp=create_mcp_server(service)
            result=asyncio.run(mcp.call_tool('ask_sql',{'question':'List item IDs','database':str(p),'allow_schema_only':True,'skills':[]}))
            if isinstance(result,tuple): result=result[1]
            if isinstance(result,dict): payload=result
            else: payload=json.loads(next(x.text for x in result if getattr(x,'type',None)=='text'))
            self.assertEqual(payload['rows'],expected)
            out=StringIO()
            with patch.object(cli,'AgentService',return_value=service),patch('sys.argv',['queryforge','--question','List item IDs','--database',str(p),'--allow-schema-only']),redirect_stdout(out):
                code=cli.main()
            self.assertEqual(code,0)
            self.assertEqual(json.loads(out.getvalue())['rows'],expected)
