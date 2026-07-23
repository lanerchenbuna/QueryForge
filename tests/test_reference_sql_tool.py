import tempfile
import unittest
from pathlib import Path

from queryforge.infrastructure.tools.reference_sql_tool import ReferenceSqlTool


class ReferenceSqlToolTest(unittest.TestCase):
    def test_finds_exact_bundled_example(self) -> None:
        database = "sample_data/anime_streaming/anime_streaming.sqlite"
        question = "What are watch hours by anime format?"
        examples = ReferenceSqlTool(database).find_similar(question)
        self.assertTrue(examples)
        self.assertEqual(examples[0].similarity, 1.0)
        self.assertIn("watch_hours", examples[0].sql)
        self.assertIn("fact_watch_session", examples[0].sql)

    def test_missing_reference_file_is_optional(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "database.sqlite"
            self.assertEqual(ReferenceSqlTool(str(database)).find_similar("test"), [])


if __name__ == "__main__":
    unittest.main()
