import datetime as dt
import io
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bidlens.cli import _database_identity, main


class DatabasePreflightCLITests(unittest.TestCase):
    def _session(self, *, guts_table=True, bidlens_schema=True):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            if bidlens_schema:
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version VALUES ('safe_revision_1')"))
                connection.execute(text("CREATE TABLE opportunities (id INTEGER PRIMARY KEY)"))
            if guts_table:
                connection.execute(text(
                    "CREATE TABLE opportunity_knowledge_brief_generations ("
                    "id INTEGER PRIMARY KEY, opportunity_id INTEGER NOT NULL, "
                    "organization_id INTEGER NOT NULL, status VARCHAR(32) NOT NULL, "
                    "created_at DATETIME NOT NULL, output_json TEXT, source_summary_json TEXT)"
                ))
        return engine, sessionmaker(bind=engine)

    def _run(self, factory):
        output = io.StringIO()
        status = main(["database-preflight"], session_factory=factory, output=output)
        return status, output.getvalue()

    def test_sqlite_preflight_reports_safe_identity_and_empty_generation_table(self):
        engine, factory = self._session()
        try:
            status, output = self._run(factory)
        finally:
            engine.dispose()
        self.assertEqual(status, 0)
        self.assertIn("backend=sqlite", output)
        self.assertIn("host=local", output)
        self.assertIn("database=:memory:", output)
        self.assertIn("warning=SQLite is a local development database", output)
        self.assertIn("guts_generation_table=true", output)
        self.assertIn("maximum_generation_id=none", output)
        self.assertIn("alembic_revision=safe_revision_1", output)

    def test_present_table_prints_only_safe_recent_generation_metadata(self):
        engine, factory = self._session()
        try:
            with engine.begin() as connection:
                for generation_id in range(1, 7):
                    connection.execute(text(
                        "INSERT INTO opportunity_knowledge_brief_generations "
                        "(id, opportunity_id, organization_id, status, created_at, output_json, source_summary_json) "
                        "VALUES (:id, :opp, :org, :status, :created, :output, :source)"
                    ), {
                        "id": generation_id, "opp": 100 + generation_id, "org": 4,
                        "status": "failed" if generation_id == 6 else "succeeded",
                        "created": dt.datetime(2026, 8, generation_id, 12, 0),
                        "output": "PRIVATE MODEL OUTPUT", "source": "PRIVATE SOURCE CONTENT",
                    })
            status, output = self._run(factory)
        finally:
            engine.dispose()
        self.assertEqual(status, 0)
        self.assertIn("maximum_generation_id=6", output)
        self.assertIn("id=6 opportunity_id=106 organization_id=4 status=failed", output)
        self.assertNotIn("id=1 opportunity_id=101", output)
        self.assertNotIn("PRIVATE MODEL OUTPUT", output)
        self.assertNotIn("PRIVATE SOURCE CONTENT", output)

    def test_guts_table_absent_is_a_safe_reachable_result(self):
        engine, factory = self._session(guts_table=False)
        try:
            status, output = self._run(factory)
        finally:
            engine.dispose()
        self.assertEqual(status, 0)
        self.assertIn("guts_generation_table=false", output)
        self.assertIn("maximum_generation_id=unavailable", output)
        self.assertIn("warning=GUTS generation table does not exist", output)

    def test_unrelated_database_is_rejected(self):
        engine, factory = self._session(guts_table=False, bidlens_schema=False)
        try:
            status, output = self._run(factory)
        finally:
            engine.dispose()
        self.assertEqual(status, 1)
        self.assertIn("warning=database does not appear to contain a BidLens schema", output)

    def test_connection_or_configuration_failure_is_safe_and_nonzero(self):
        secret = "DO-NOT-PRINT-PASSWORD"

        def failing_factory():
            raise RuntimeError(f"postgresql://user:{secret}@private.example/railway")

        status, output = self._run(failing_factory)
        self.assertEqual(status, 1)
        self.assertIn("database could not be reached", output)
        self.assertNotIn(secret, output)
        self.assertNotIn("postgresql://", output)

    def test_postgresql_identity_sanitizes_credentials_and_query_parameters(self):
        engine = create_engine(
            "postgresql://private_user:private_password@db.example.test:6543/railway"
            "?sslmode=require&sslkey=private-key"
        )
        try:
            identity = _database_identity(engine)
        finally:
            engine.dispose()
        self.assertEqual(identity, {
            "backend": "postgresql", "host": "db.example.test",
            "port": 6543, "database": "railway",
        })
        rendered = repr(identity)
        self.assertNotIn("private_user", rendered)
        self.assertNotIn("private_password", rendered)
        self.assertNotIn("sslmode", rendered)
        self.assertNotIn("private-key", rendered)

    def test_existing_cli_parser_behavior_remains_available(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            main(["generate-guts", "--help"], output=output)
        self.assertEqual(raised.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
