import importlib.util
import unittest
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from bidlens.models import OpportunityKnowledgeBriefGeneration


MIGRATION_PATH = Path("alembic/versions/3b4c5d6e7f8a_add_guts_persistence.py")


class GutsMigrationTests(unittest.TestCase):
    def test_migration_is_single_head_successor_and_contains_reversible_tables(self):
        spec = importlib.util.spec_from_file_location("guts_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, "3b4c5d6e7f8a")
        self.assertEqual(module.down_revision, "2a3b4c5d6e7f")
        text = MIGRATION_PATH.read_text()
        for table in (
            "opportunity_knowledge_brief_generations",
            "opportunity_knowledge_brief_statements",
            "opportunity_knowledge_brief_sources",
            "opportunity_knowledge_brief_statement_sources",
        ):
            self.assertIn(table, text)
        self.assertIn("def downgrade()", text)

    def test_model_compiles_postgresql_partial_unique_active_index(self):
        index = next(
            item for item in OpportunityKnowledgeBriefGeneration.__table__.indexes
            if item.name == "uq_guts_generation_active_org_opp"
        )
        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        self.assertIn("CREATE UNIQUE INDEX", ddl)
        self.assertIn("WHERE status IN ('pending', 'running')", ddl)


if __name__ == "__main__":
    unittest.main()
