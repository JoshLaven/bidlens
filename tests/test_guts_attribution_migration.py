import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from bidlens.models import OpportunityKnowledgeBriefStatement


MIGRATION_PATH = Path(
    "alembic/versions/5d6e7f8a9b0c_add_guts_statement_attribution.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("guts_attribution_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GUTSAttributionMigrationTests(unittest.TestCase):
    def test_migration_is_additive_nullable_reversible_single_head_successor(self):
        module = load_migration()
        self.assertEqual(module.revision, "5d6e7f8a9b0c")
        self.assertEqual(module.down_revision, "4c5d6e7f8a9b")
        with patch.object(module.op, "add_column") as add_column:
            module.upgrade()
        table, column = add_column.call_args.args
        self.assertEqual(table, "opportunity_knowledge_brief_statements")
        self.assertEqual(column.name, "attribution_json")
        self.assertTrue(column.nullable)
        self.assertIsNone(column.server_default)
        with patch.object(module.op, "drop_column") as drop_column:
            module.downgrade()
        drop_column.assert_called_once_with(
            "opportunity_knowledge_brief_statements", "attribution_json",
        )

    def test_model_column_is_nullable_json_without_index(self):
        column = OpportunityKnowledgeBriefStatement.__table__.c.attribution_json
        self.assertTrue(column.nullable)
        self.assertIsNone(column.server_default)
        self.assertEqual(column.type.__class__.__name__, "JSON")
        self.assertFalse(any(
            "attribution_json" in {item.name for item in index.columns}
            for index in OpportunityKnowledgeBriefStatement.__table__.indexes
        ))


if __name__ == "__main__":
    unittest.main()
