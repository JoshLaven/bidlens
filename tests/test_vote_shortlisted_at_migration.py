import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from bidlens.models import Vote


MIGRATION_PATH = Path("alembic/versions/7f8a9b0c1d2e_add_vote_shortlisted_at.py")


class VoteShortlistedAtMigrationTests(unittest.TestCase):
    def test_model_field_is_nullable_and_timezone_aware(self):
        column = Vote.__table__.c.shortlisted_at
        self.assertTrue(column.nullable)
        self.assertTrue(column.type.timezone)

    def test_migration_is_additive_nullable_reversible_and_does_not_backfill(self):
        spec = importlib.util.spec_from_file_location("vote_shortlisted_at_migration", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertEqual(module.down_revision, "6e7f8a9b0c1d")
        with (
            patch.object(module.op, "add_column") as add_column,
            patch.object(module.op, "create_index") as create_index,
            patch.object(module.op, "execute") as execute,
        ):
            module.upgrade()
        column = add_column.call_args.args[1]
        self.assertEqual(column.name, "shortlisted_at")
        self.assertTrue(column.nullable)
        self.assertTrue(column.type.timezone)
        create_index.assert_called_once_with(
            "ix_votes_shortlisted_at", "votes", ["shortlisted_at"], unique=False
        )
        execute.assert_not_called()

        with (
            patch.object(module.op, "drop_index") as drop_index,
            patch.object(module.op, "drop_column") as drop_column,
        ):
            module.downgrade()
        drop_index.assert_called_once_with("ix_votes_shortlisted_at", table_name="votes")
        drop_column.assert_called_once_with("votes", "shortlisted_at")


if __name__ == "__main__":
    unittest.main()
