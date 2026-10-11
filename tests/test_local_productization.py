"""Local lifecycle, asset version invalidation and optional skill-call budgets."""
import os
import importlib.util
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.domain.skills import SkillManager, SkillRegistry, SkillRegistryError
from queryforge.infrastructure.storage.report_store import ReportStore
from queryforge.workflow.node.skill_selection_node import SkillSelectionNode
from tests.test_skill_selection_node import StaticSelectorLLM, sample_context


class LocalProductizationTest(unittest.TestCase):
    def test_simple_count_needs_no_skill_router_call(self):
        llm = StaticSelectorLLM(error=AssertionError("No optional skill is relevant"))
        context = sample_context("How many rows? Return only n.")
        self.assertTrue(SkillSelectionNode(llm, SkillManager()).execute(context).success)
        self.assertEqual(llm.calls, 0)
        self.assertEqual(context.loaded_skill_names, ["sql_best_practices"])
        self.assertEqual(context.skill_selection_mode, "auto")
        self.assertFalse(context.task_context["skill_prefilter"]["router_called"])

    def test_router_cannot_load_prefiltered_out_skill(self):
        llm = StaticSelectorLLM({"skills": ["business_rules", "data_quality_testing"]})
        context = sample_context("Count rows with NULL values")
        self.assertTrue(SkillSelectionNode(llm, SkillManager()).execute(context).success)
        self.assertEqual(context.loaded_skill_names, ["sql_best_practices", "data_quality_testing"])
        self.assertNotIn('name="business_rules"', llm.prompt)

    def test_content_cache_reuses_same_version_and_invalidates_same_mtime_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); skill = root / "local_example"; skill.mkdir()
            metadata = skill / "skill.yml"
            metadata.write_text("name: local_example\ndescription: Local example\nallowed_nodes: [gen_sql]\nenabled: true\npriority: 1\n")
            instructions = skill / "SKILL.md"; instructions.write_text("Version one")
            manager = SkillManager(SkillRegistry(root))
            first = manager.context_for_node("gen_sql")
            self.assertIs(first, manager.context_for_node("gen_sql"))
            before = instructions.stat(); instructions.write_text("Version two")
            os.utime(instructions, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = manager.context_for_node("gen_sql")
            self.assertIsNot(first, second)
            self.assertIn("Version two", second.loaded_skills)
            before = metadata.stat(); metadata.write_text(metadata.read_text().replace("enabled: true", "enabled: false"))
            os.utime(metadata, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(manager.context_for_node("gen_sql").loaded_skill_names, ())
            instructions.unlink()
            with self.assertRaises(SkillRegistryError): manager.context_for_node("gen_sql")

    def test_skill_context_cache_separates_nodes_and_explicit_selection(self):
        manager = SkillManager()
        all_defaults = manager.context_for_node("gen_sql")
        self.assertEqual(manager.context_for_node("gen_sql", []).loaded_skill_names, ())
        self.assertNotEqual(all_defaults, manager.context_for_node("reflect"))
        self.assertLessEqual(len(manager._context_cache), 32)

    def test_reports_expire_and_cleanup_quarantines_both_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ReportStore(directory, 1)
            html, manifest = store.paths("run_old")
            html.write_text("old report"); manifest.write_text('{"evidence": "preserved"}')
            now = time.time(); os.utime(html, (now - 90000, now - 90000))
            with self.assertRaisesRegex(ValueError, "expired"): store.report_path("run_old", now=now)
            self.assertEqual(store.expire(now=now)["expired"], ["run_old"])
            self.assertTrue(html.exists())
            self.assertEqual(store.expire(dry_run=False, now=now)["action"], "quarantined")
            self.assertFalse(html.exists())
            retained = list((Path(directory) / ".expired").glob("*/*"))
            self.assertEqual({path.name for path in retained}, {"run_old.html", "run_old.manifest.json"})
            self.assertTrue(any("preserved" in path.read_text() for path in retained))

    def test_report_symlink_and_path_traversal_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); store = ReportStore(root / "reports")
            store.root.mkdir(); secret = root / "outside.html"; secret.write_text("outside")
            (store.root / "run.html").symlink_to(secret)
            for run_id in ("run", "../outside", "x/y", ""):
                with self.subTest(run_id=run_id), self.assertRaises(ValueError): store.report_path(run_id)
            self.assertEqual(len(store.expire(dry_run=False)["skipped"]), 1)
            self.assertEqual(secret.read_text(), "outside")

    def test_repeated_skill_metadata_is_not_reparsed(self):
        registry = SkillRegistry()
        with patch.object(registry, "_read_metadata", wraps=registry._read_metadata) as parser:
            registry.list_skills(); first_count = parser.call_count
            for _ in range(10): registry.list_skills()
        self.assertEqual(first_count, 10)
        self.assertEqual(parser.call_count, first_count)

    @unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI not installed")
    def test_local_api_denies_foreign_origin_before_publication(self):
        from fastapi.testclient import TestClient
        from queryforge.interfaces.api.app import create_app
        from tests.studio_fixture_server import fixture_service
        with tempfile.TemporaryDirectory() as directory:
            client = TestClient(create_app(fixture_service(Path(directory))), base_url="http://127.0.0.1:8000")
            response = client.post("/domains/local_fixture/publish", headers={"Origin": "https://untrusted.example"})
            self.assertEqual(response.status_code, 403)
            self.assertIn("Cross-origin", response.json()["detail"])
            # Loopback CLI requests with no browser Origin still reach the
            # endpoint's normal contract validation.
            response = client.post("/domains/local_fixture/publish")
            self.assertEqual(response.status_code, 400)
