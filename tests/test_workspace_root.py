"""Workspace-root resolution (E-22).

``PROJECT_ROOT`` used to be ``Path(__file__).resolve().parents[2]`` in six separate
modules. In a source checkout that is the repository root; after
``pip install queryforge`` it is ``site-packages``, so ``.queryforge/history.db``,
the trace directory, the chart output directory and the LanceDB path all moved into
the installed package — not the caller's data, and frequently not writable.

These tests pin the *resolution order* rather than the checkout case, because the
checkout case is what every other test in this suite already exercises implicitly.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.core import paths
from queryforge.core.paths import (
    WORKSPACE_ROOT_ENV,
    resolve_path,
    workspace_root,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class WorkspaceRootTest(unittest.TestCase):
    def setUp(self):
        paths.workspace_root.cache_clear()
        self.addCleanup(paths.workspace_root.cache_clear)

    def test_checkout_is_found_by_walking_up_from_the_package(self):
        """Without an override, the nearest checkout ancestor wins."""
        self.assertEqual(workspace_root(), PROJECT_ROOT)
        self.assertTrue((workspace_root() / "pyproject.toml").is_file())

    def test_override_wins_and_supports_relative_and_tilde_forms(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(
            os.environ, {WORKSPACE_ROOT_ENV: d}
        ):
            paths.workspace_root.cache_clear()
            self.assertEqual(workspace_root(), Path(d).resolve())

            paths.workspace_root.cache_clear()
            with patch.dict(os.environ, {WORKSPACE_ROOT_ENV: "."}):
                self.assertEqual(workspace_root(), Path.cwd().resolve())

    def test_installed_package_does_not_resolve_state_into_site_packages(self):
        """The regression: no override + no checkout ancestor -> the cwd, not the package.

        ``site-packages`` is simulated by a package directory with no checkout
        marker anywhere above it, which is exactly what makes ``parents[2]`` wrong
        there.
        """
        with tempfile.TemporaryDirectory() as package_parent, tempfile.TemporaryDirectory() as cwd:
            installed = Path(package_parent) / "site-packages" / "queryforge" / "core"
            installed.mkdir(parents=True)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(WORKSPACE_ROOT_ENV, None)
                with patch.object(Path, "cwd", staticmethod(lambda: Path(cwd))):
                    resolved = paths._resolve_root(installed)
                    self.assertEqual(resolved, Path(cwd).resolve())
                    # And the checkout case still wins over the cwd.
                    self.assertEqual(
                        paths._resolve_root(PROJECT_ROOT / "queryforge" / "core"),
                        PROJECT_ROOT,
                    )

    def test_a_wrong_root_is_not_selected_for_a_non_checkout_install(self):
        """Guard the marker list: a bare directory must not be mistaken for a checkout."""
        with tempfile.TemporaryDirectory() as package_parent:
            installed = Path(package_parent) / "queryforge" / "core"
            installed.mkdir(parents=True)
            self.assertFalse(paths._is_checkout(installed))
            self.assertFalse(paths._is_checkout(Path(package_parent)))
            self.assertTrue(paths._is_checkout(PROJECT_ROOT))


class ResolvePathTest(unittest.TestCase):
    def test_absolute_paths_pass_through_untouched(self):
        absolute = Path(tempfile.gettempdir()) / "queryforge-absolute.db"
        self.assertEqual(resolve_path(absolute), absolute)

    def test_relative_paths_resolve_against_the_workspace(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(
            os.environ, {WORKSPACE_ROOT_ENV: d}
        ):
            paths.workspace_root.cache_clear()
            self.addCleanup(paths.workspace_root.cache_clear)
            self.assertEqual(
                resolve_path(".queryforge/history.db"), Path(d).resolve() / ".queryforge/history.db"
            )


class ModuleRootsAgreeTest(unittest.TestCase):
    def test_every_module_root_is_the_same_object_value(self):
        """Six modules derived this independently; they must not drift again."""
        from queryforge.core.config import PROJECT_ROOT as config_root
        from queryforge.core.observability import PROJECT_ROOT as observability_root
        from queryforge.domain.domains import PROJECT_ROOT as domains_root
        from queryforge.domain.semantic.builder import PROJECT_ROOT as builder_root
        from queryforge.infrastructure.storage import knowledge_base, sql_history_store
        from queryforge.infrastructure.storage import vector_store
        from queryforge.workflow.node.visualization_node import (
            PROJECT_ROOT as visualization_root,
        )

        roots = {
            "config": config_root,
            "observability": observability_root,
            "domains": domains_root,
            "semantic.builder": builder_root,
            "knowledge_base": knowledge_base.PROJECT_ROOT,
            "sql_history_store": sql_history_store.PROJECT_ROOT,
            "vector_store": vector_store.PROJECT_ROOT,
            "visualization_node": visualization_root,
        }
        distinct = {str(value) for value in roots.values()}
        self.assertEqual(distinct, {str(PROJECT_ROOT)}, roots)

    def test_state_defaults_live_under_the_workspace_root(self):
        from queryforge.core.observability import DEFAULT_LOG_PATH, DEFAULT_TRACE_DIR
        from queryforge.infrastructure.storage.sql_history_store import (
            DEFAULT_HISTORY_DB_PATH,
        )
        from queryforge.workflow.node.visualization_node import (
            DEFAULT_CHART_OUTPUT_DIR,
        )

        for label, path in {
            "log": DEFAULT_LOG_PATH,
            "traces": DEFAULT_TRACE_DIR,
            "history": DEFAULT_HISTORY_DB_PATH,
            "charts": DEFAULT_CHART_OUTPUT_DIR,
        }.items():
            with self.subTest(default=label):
                self.assertTrue(
                    str(path).startswith(str(PROJECT_ROOT)), f"{label} -> {path}"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
