"""Ensure the restructuring keeps public legacy imports usable during migration."""

from __future__ import annotations

import unittest


class LegacyImportTest(unittest.TestCase):
    def test_legacy_namespaces_resolve_to_current_implementations(self):
        from queryforge.agent.workflow_runner import WorkflowRunner
        from queryforge.agent_team.agents.entry_router import EntryRouterAgent
        from queryforge.api.app import create_app
        from queryforge.config import Config
        from queryforge.db.sqlite_connector import SQLiteConnector
        from queryforge.mcp.server import create_mcp_server
        from queryforge.service import AgentService

        self.assertEqual(WorkflowRunner.__name__, "WorkflowRunner")
        self.assertEqual(EntryRouterAgent.__name__, "EntryRouterAgent")
        self.assertEqual(create_app.__name__, "create_app")
        self.assertEqual(Config.__name__, "Config")
        self.assertEqual(SQLiteConnector.__name__, "SQLiteConnector")
        self.assertEqual(create_mcp_server.__name__, "create_mcp_server")
        self.assertEqual(AgentService.__name__, "AgentService")


if __name__ == "__main__":
    unittest.main()
