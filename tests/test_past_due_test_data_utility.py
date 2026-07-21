import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts import repair_past_due_test_data as utility


class PastDueTestDataUtilityTests(unittest.TestCase):
    def _db(self):
        tmpdir = tempfile.TemporaryDirectory()
        path = Path(tmpdir.name) / "bidlens.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE opportunities (
                id INTEGER PRIMARY KEY,
                organization_id INTEGER NOT NULL,
                source_record_id TEXT NOT NULL,
                title TEXT NOT NULL,
                response_deadline TEXT,
                qualification_status TEXT NOT NULL,
                decision_state TEXT NOT NULL
            );
            CREATE TABLE votes (
                id INTEGER PRIMARY KEY,
                org_id INTEGER NOT NULL,
                opp_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE opportunity_outcomes (
                id INTEGER PRIMARY KEY,
                organization_id INTEGER NOT NULL,
                opportunity_id INTEGER NOT NULL
            );
            """
        )
        return tmpdir, path, conn

    def test_rejects_non_sqlite_database_url(self):
        with self.assertRaises(ValueError):
            utility.sqlite_path_from_database_url("postgresql://example/prod")

    def test_repair_updates_only_labeled_pursue_votes(self):
        tmpdir, _path, conn = self._db()
        self.addCleanup(tmpdir.cleanup)
        conn.execute(
            "INSERT INTO opportunities VALUES (1, 10, 'BL-PAST-DUE-TEST-1', 'Past', '2026-07-01', 'qualified', 'INBOX')"
        )
        conn.execute(
            "INSERT INTO opportunities VALUES (2, 10, 'REAL-1', 'Real', '2026-07-01', 'qualified', 'INBOX')"
        )
        conn.execute("INSERT INTO votes VALUES (1, 10, 1, 20, 'PURSUE', '2026-07-10T12:00:00+00:00')")
        conn.execute("INSERT INTO votes VALUES (2, 10, 2, 20, 'PURSUE', '2026-07-10T12:00:00+00:00')")
        conn.commit()

        opportunities = utility.find_test_opportunities(conn)
        result = utility.repair_vote_timestamps(conn, opportunities, apply=True)

        self.assertEqual(len(result["repaired"]), 1)
        self.assertEqual(
            conn.execute("SELECT updated_at FROM votes WHERE id = 1").fetchone()["updated_at"],
            "2026-06-30T12:00:00+00:00",
        )
        self.assertEqual(
            conn.execute("SELECT updated_at FROM votes WHERE id = 2").fetchone()["updated_at"],
            "2026-07-10T12:00:00+00:00",
        )

    def test_cleanup_deletes_only_labeled_records_and_related_rows(self):
        tmpdir, _path, conn = self._db()
        self.addCleanup(tmpdir.cleanup)
        conn.execute(
            "INSERT INTO opportunities VALUES (1, 10, 'BL-PAST-DUE-TEST-1', 'Past', '2026-07-01', 'qualified', 'INBOX')"
        )
        conn.execute(
            "INSERT INTO opportunities VALUES (2, 10, 'REAL-1', 'Real', '2026-07-01', 'qualified', 'INBOX')"
        )
        conn.execute("INSERT INTO votes VALUES (1, 10, 1, 20, 'PURSUE', '2026-07-10T12:00:00+00:00')")
        conn.execute("INSERT INTO votes VALUES (2, 10, 2, 20, 'PURSUE', '2026-07-10T12:00:00+00:00')")
        conn.commit()

        result = utility.cleanup_test_records(conn, utility.find_test_opportunities(conn), apply=True)

        self.assertEqual(result["deleted_opportunities"], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT source_record_id FROM opportunities").fetchone()[0], "REAL-1")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
