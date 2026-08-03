import importlib.util
import unittest
from pathlib import Path


MIGRATION_PATH = Path("alembic/versions/6e7f8a9b0c1d_add_team_summary_metadata.py")


class TeamSummaryMigrationTests(unittest.TestCase):
    def test_migration_is_additive_reversible_single_head_successor(self):
        spec = importlib.util.spec_from_file_location("team_summary_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, "6e7f8a9b0c1d")
        self.assertEqual(module.down_revision, "5d6e7f8a9b0c")
        text = MIGRATION_PATH.read_text(encoding="utf-8")
        for column in (
            "evidence_fingerprint", "input_contract_version", "prompt_version",
            "note_count_included", "note_count_available",
            "latest_note_timestamp_included",
        ):
            self.assertIn(column, text)
        self.assertNotIn("create_table", text)
        self.assertIn("def downgrade()", text)


if __name__ == "__main__":
    unittest.main()
