import io
import sqlite3
import sys
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main as cli
from queryforge.core.config import Config


ANIME_ROOT = Path("sample_data/anime_streaming").resolve()
DATABASE = ANIME_ROOT / "anime_streaming.sqlite"


class DeliveryTest(unittest.TestCase):
    def test_cli_skill_modes_parse_auto_none_and_manual(self) -> None:
        self.assertIsNone(cli._parse_skill_names(None))
        self.assertIsNone(cli._parse_skill_names("auto"))
        self.assertEqual(cli._parse_skill_names("none"), [])
        self.assertEqual(
            cli._parse_skill_names("business_rules,sql_best_practices"),
            ["business_rules", "sql_best_practices"],
        )

    def test_cli_rejects_out_of_range_retry_count(self) -> None:
        stderr = io.StringIO()
        argv = [
            "main.py",
            "--max-retries",
            "11",
            "--question",
            "How many anime titles are there?",
        ]
        with patch.object(sys, "argv", argv), redirect_stderr(stderr):
            exit_code = cli.main()
        self.assertEqual(exit_code, 2)
        self.assertIn("between 0 and 10", stderr.getvalue())

    def test_bundled_sample_has_required_nonempty_tables_and_artifacts(self) -> None:
        self.assertTrue(DATABASE.is_file())
        with closing(
            sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True)
        ) as connection:
            for table in (
                "dim_anime",
                "dim_episode",
                "dim_user",
                "fact_watch_session",
                "fact_rating",
            ):
                with self.subTest(table=table):
                    count = connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0]
                    self.assertGreater(count, 0)
        sample_root = DATABASE.parent
        self.assertTrue((sample_root / "success_story.csv").is_file())
        self.assertTrue(any((sample_root / "reference_sql").glob("*.sql")))

    def test_cli_without_api_key_is_friendly(self) -> None:
        config = Config(
            llm_provider="qwen",
            llm_api_key=None,
            llm_model="qwen-plus",
            llm_base_url="https://example.invalid/v1",
            database_path=str(DATABASE),
            api_key_env_names=("QWEN_API_KEY", "DASHSCOPE_API_KEY"),
        )
        stderr = io.StringIO()
        argv = ["main.py", "--question", "How many anime titles are there?"]
        with patch.object(sys, "argv", argv), patch.object(
            cli, "load_config", return_value=config
        ), redirect_stderr(stderr):
            exit_code = cli.main()
        message = stderr.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertIn("QWEN_API_KEY", message)
        self.assertNotIn("Traceback", message)

    def test_anime_sample_has_diverse_relational_tables_and_artifacts(self) -> None:
        expected = {
            "dim_date",
            "dim_studio",
            "dim_genre",
            "dim_anime",
            "bridge_anime_genre",
            "dim_episode",
            "dim_user",
            "fact_subscription",
            "fact_watch_session",
            "fact_rating",
            "fact_ad_impression",
            "fact_user_follow",
            "dim_merch_product",
            "fact_merch_order",
            "fact_merch_order_item",
        }
        self.assertTrue(DATABASE.is_file())
        with closing(sqlite3.connect(DATABASE)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(expected.issubset(tables))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            for table in expected:
                self.assertGreater(
                    connection.execute(
                        f'SELECT COUNT(*) FROM "{table}"'
                    ).fetchone()[0],
                    0,
                )
            self.assertGreater(
                connection.execute(
                    "SELECT SUM(row_count) FROM ("
                    + " UNION ALL ".join(
                        f"SELECT COUNT(*) AS row_count FROM {table}"
                        for table in expected
                    )
                    + ")"
                ).fetchone()[0],
                350_000,
            )
        self.assertTrue((ANIME_ROOT / "semantic_model.yml").is_file())
        self.assertTrue((ANIME_ROOT / "subjects.yml").is_file())
        self.assertTrue((ANIME_ROOT / "sql_policy.yml").is_file())

    def test_cli_lists_all_configured_model_providers(self) -> None:
        stdout = io.StringIO()
        argv = ["main.py", "--list-models"]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            exit_code = cli.main()
        listing = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        for provider in ("openai", "claude", "gemini", "deepseek", "qwen", "glm"):
            with self.subTest(provider=provider):
                self.assertIn(provider, listing)

    def test_cli_lists_local_skills_without_api_key(self) -> None:
        stdout = io.StringIO()
        argv = ["main.py", "--list-skills"]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            exit_code = cli.main()
        listing = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("sql_best_practices", listing)
        self.assertIn("business_rules", listing)
        self.assertIn("data_quality_testing", listing)


if __name__ == "__main__":
    unittest.main()
