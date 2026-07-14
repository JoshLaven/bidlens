import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _drop_bidlens_modules(*module_names: str) -> None:
    for module_name in module_names:
        sys.modules.pop(module_name, None)
    package = sys.modules.get("bidlens")
    if package is not None:
        for module_name in module_names:
            if module_name.startswith("bidlens."):
                attr = module_name.rsplit(".", 1)[1]
                if hasattr(package, attr):
                    delattr(package, attr)


class DatabaseConfigTests(unittest.TestCase):
    def test_database_url_normalizes_hosted_postgres_scheme(self):
        _drop_bidlens_modules("bidlens.config")
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgres://user:secret@example.com:5432/bidlens"},
            clear=False,
        ):
            config = importlib.import_module("bidlens.config")

        self.assertEqual(
            config.DATABASE_URL,
            "postgresql://user:secret@example.com:5432/bidlens",
        )
        self.assertEqual(config.DATABASE_SCHEME, "postgresql")

    def test_safe_database_url_hides_passwords(self):
        _drop_bidlens_modules("bidlens.config")
        config = importlib.import_module("bidlens.config")

        safe_url = config.safe_database_url("postgresql://user:secret@example.com:5432/bidlens")

        self.assertIn("***", safe_url)
        self.assertNotIn("secret", safe_url)

    def test_database_engine_uses_postgres_url_without_sqlite_options(self):
        _drop_bidlens_modules("bidlens.database", "bidlens.config")
        with patch.dict(
            os.environ,
            {"DATABASE_URL": "postgres://user:secret@example.com:5432/bidlens"},
            clear=False,
        ):
            database = importlib.import_module("bidlens.database")

        self.assertEqual(database.engine.url.get_backend_name(), "postgresql")
        self.assertEqual(database.engine.dialect.driver, "psycopg2")
        self.assertTrue(getattr(database.engine.pool, "_pre_ping", False))

    def test_alembic_env_reads_bidlens_database_url(self):
        env_source = Path("alembic/env.py").read_text()

        self.assertIn("from src.bidlens.config import DATABASE_URL", env_source)
        self.assertIn('config.set_main_option("sqlalchemy.url", DATABASE_URL)', env_source)


if __name__ == "__main__":
    unittest.main()
