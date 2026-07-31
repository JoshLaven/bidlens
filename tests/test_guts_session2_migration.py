import importlib.util
import unittest
from pathlib import Path

from bidlens.models import OpportunitySourceMaterialExtraction


MIGRATION_PATH = Path("alembic/versions/4c5d6e7f8a9b_add_source_material_extractions.py")


class GutsSession2MigrationTests(unittest.TestCase):
    def test_migration_follows_session_one_and_is_reversible(self):
        spec = importlib.util.spec_from_file_location("guts_session2_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.revision, "4c5d6e7f8a9b")
        self.assertEqual(module.down_revision, "3b4c5d6e7f8a")
        text = MIGRATION_PATH.read_text()
        self.assertIn("opportunity_source_material_extractions", text)
        self.assertIn("uq_source_material_extraction_cache_key", text)
        self.assertIn("def downgrade()", text)

    def test_model_has_required_cache_constraints_without_source_body_duplication(self):
        columns = set(OpportunitySourceMaterialExtraction.__table__.columns.keys())
        self.assertIn("extracted_text", columns)
        self.assertNotIn("raw_source_text", columns)
        unique_names = {
            constraint.name for constraint in OpportunitySourceMaterialExtraction.__table__.constraints
            if constraint.name
        }
        self.assertIn("uq_source_material_extraction_cache_key", unique_names)


if __name__ == "__main__":
    unittest.main()
