#!/usr/bin/env python3
"""Repair or clean up local-only Past Due outcome test data.

This utility is intentionally narrow:
- It only supports SQLite DATABASE_URL values.
- It only touches opportunities whose source_record_id starts with
  BL-PAST-DUE-TEST-.
- It repairs the timestamp used by BidLens Past Due eligibility
  (votes.updated_at). If a local votes.created_at column exists, it updates
  that too for compatibility with local scratch schemas.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse


TEST_PREFIX = "BL-PAST-DUE-TEST-"
DEFAULT_DATABASE_URL = "sqlite:///./bidlens.db"


@dataclass(frozen=True)
class TestOpportunity:
    id: int
    organization_id: int
    source_record_id: str
    title: str
    response_deadline: str
    qualification_status: str
    decision_state: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def sqlite_path_from_database_url(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Refusing to run: DATABASE_URL must use sqlite for this local-only utility.")
    if parsed.netloc:
        raise ValueError("Refusing to run: remote SQLite URLs are not supported.")
    if parsed.path in {"", "/"}:
        raise ValueError("Refusing to run: SQLite database path is empty.")

    raw_path = unquote(parsed.path)
    if raw_path.startswith("/") and not database_url.startswith("sqlite:////"):
        # sqlite:///./bidlens.db parses as /./bidlens.db; prefer the literal
        # relative path for three-slash URLs.
        raw_path = raw_path.lstrip("/")
    path = Path(raw_path)
    if not path.is_absolute():
        path = (_repo_root() / path).resolve()
    return path.resolve()


def enforce_local_only(database_url: str, db_path: Path) -> None:
    if not database_url.startswith("sqlite:"):
        raise ValueError("Refusing to run outside local SQLite.")
    if _truthy(os.getenv("SESSION_COOKIE_SECURE")):
        raise ValueError("Refusing to run: SESSION_COOKIE_SECURE=true looks like a hosted environment.")
    if os.getenv("AUTO_CREATE_SCHEMA") == "false":
        raise ValueError("Refusing to run: AUTO_CREATE_SCHEMA=false looks like a hosted/migrated environment.")

    repo_root = _repo_root().resolve()
    allowed_roots = {repo_root, Path("/private/tmp").resolve(), Path("/tmp").resolve()}
    if not any(db_path == root or root in db_path.parents for root in allowed_roots):
        raise ValueError(f"Refusing to run: {db_path} is outside approved local paths.")
    if not db_path.exists():
        raise ValueError(f"Refusing to run: SQLite database does not exist: {db_path}")


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return bool(row)


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    if not table_exists(conn, table_name):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_name})")}


def require_tables(conn: sqlite3.Connection) -> None:
    missing = [name for name in ("opportunities", "votes") if not table_exists(conn, name)]
    if missing:
        raise ValueError(f"Refusing to run: database is missing required table(s): {', '.join(missing)}")


def find_test_opportunities(conn: sqlite3.Connection) -> list[TestOpportunity]:
    rows = conn.execute(
        """
        SELECT id, organization_id, source_record_id, title, response_deadline,
               qualification_status, decision_state
          FROM opportunities
         WHERE source_record_id LIKE ?
         ORDER BY source_record_id ASC
        """,
        (f"{TEST_PREFIX}%",),
    ).fetchall()
    return [TestOpportunity(**dict(row)) for row in rows]


def repair_vote_timestamps(conn: sqlite3.Connection, opportunities: list[TestOpportunity], *, apply: bool) -> dict:
    vote_columns = table_columns(conn, "votes")
    update_created_at = "created_at" in vote_columns
    repaired = []
    missing_votes = []
    ineligible_records = []

    for opp in opportunities:
        if (
            opp.qualification_status != "qualified"
            or opp.decision_state == "ARCHIVED"
            or not opp.response_deadline
        ):
            ineligible_records.append(opp.source_record_id)
            continue
        vote_rows = conn.execute(
            """
            SELECT id, updated_at
              FROM votes
             WHERE org_id = ?
               AND opp_id = ?
               AND vote = 'PURSUE'
             ORDER BY id ASC
            """,
            (opp.organization_id, opp.id),
        ).fetchall()
        if not vote_rows:
            missing_votes.append(opp.source_record_id)
            continue

        deadline = date.fromisoformat(str(opp.response_deadline)[:10])
        repaired_timestamp = datetime.combine(
            deadline - timedelta(days=1),
            time(12, 0),
            tzinfo=timezone.utc,
        ).isoformat()
        for vote in vote_rows:
            if apply:
                if update_created_at:
                    conn.execute(
                        "UPDATE votes SET updated_at = ?, created_at = ? WHERE id = ?",
                        (repaired_timestamp, repaired_timestamp, vote["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE votes SET updated_at = ? WHERE id = ?",
                        (repaired_timestamp, vote["id"]),
                    )
            repaired.append({
                "source_record_id": opp.source_record_id,
                "vote_id": vote["id"],
                "timestamp": repaired_timestamp,
            })

    if apply:
        conn.commit()
    return {
        "repaired": repaired,
        "missing_votes": missing_votes,
        "ineligible_records": ineligible_records,
        "updated_created_at": update_created_at,
    }


def unresolved_eligible_records(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    outcome_clause = ""
    if table_exists(conn, "opportunity_outcomes"):
        outcome_clause = """
          AND NOT EXISTS (
                SELECT 1
                  FROM opportunity_outcomes oo
                 WHERE oo.organization_id = o.organization_id
                   AND oo.opportunity_id = o.id
              )
        """
    return conn.execute(
        f"""
        SELECT o.source_record_id, o.title, o.response_deadline, o.organization_id,
               MIN(v.updated_at) AS pursue_timestamp
          FROM opportunities o
          JOIN votes v
            ON v.org_id = o.organization_id
           AND v.opp_id = o.id
           AND v.vote = 'PURSUE'
         WHERE o.source_record_id LIKE ?
           AND o.qualification_status = 'qualified'
           AND o.decision_state != 'ARCHIVED'
           AND o.response_deadline IS NOT NULL
           AND date(o.response_deadline) < date('now')
           AND date(v.updated_at) <= date(o.response_deadline)
           {outcome_clause}
         GROUP BY o.id
         ORDER BY o.source_record_id ASC
        """,
        (f"{TEST_PREFIX}%",),
    ).fetchall()


def cleanup_test_records(conn: sqlite3.Connection, opportunities: list[TestOpportunity], *, apply: bool) -> dict:
    opp_ids = [opp.id for opp in opportunities]
    if not opp_ids:
        return {"deleted_opportunities": 0, "deleted_related_rows": {}}
    placeholders = ",".join("?" for _ in opp_ids)
    related_tables = {
        "opportunity_outcomes": "opportunity_id",
        "opportunity_history_recipients": "opportunity_id",
        "opportunity_history_events": "opportunity_id",
        "opportunity_pursuit_lane_matches": "opportunity_id",
        "user_opportunities": "opportunity_id",
        "opportunity_notes": "opportunity_id",
        "events": "opp_id",
        "votes": "opp_id",
    }
    deleted: dict[str, int] = {}
    if apply:
        for table, column in related_tables.items():
            if not table_exists(conn, table):
                continue
            cursor = conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", opp_ids)
            deleted[table] = cursor.rowcount
        cursor = conn.execute(f"DELETE FROM opportunities WHERE id IN ({placeholders})", opp_ids)
        conn.commit()
        deleted_opportunities = cursor.rowcount
    else:
        for table, column in related_tables.items():
            if table_exists(conn, table):
                deleted[table] = conn.execute(
                    f"SELECT COUNT(*) AS count FROM {table} WHERE {column} IN ({placeholders})",
                    opp_ids,
                ).fetchone()["count"]
        deleted_opportunities = len(opp_ids)
    return {"deleted_opportunities": deleted_opportunities, "deleted_related_rows": deleted}


def print_report(label: str, rows: list[sqlite3.Row]) -> None:
    print(label)
    if not rows:
        print("  none")
        return
    for row in rows:
        print(
            "  "
            f"{row['source_record_id']} | due {row['response_deadline']} | "
            f"pursue timestamp {row['pursue_timestamp']} | org {row['organization_id']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply timestamp repairs.")
    mode.add_argument("--cleanup", action="store_true", help="Delete BL-PAST-DUE-TEST-* records and related rows.")
    args = parser.parse_args(argv)

    try:
        db_path = sqlite_path_from_database_url(args.database_url)
        enforce_local_only(args.database_url, db_path)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            require_tables(conn)
            opportunities = find_test_opportunities(conn)
            print("BidLens Past Due local test-data utility")
            print(f"Database: {db_path}")
            print(f"Matched test records: {len(opportunities)}")
            for opp in opportunities:
                print(f"  {opp.source_record_id} | due {opp.response_deadline} | {opp.qualification_status}/{opp.decision_state}")

            if args.cleanup:
                result = cleanup_test_records(conn, opportunities, apply=True)
                print(f"Cleanup complete. Deleted opportunities: {result['deleted_opportunities']}")
                print(f"Related rows: {result['deleted_related_rows']}")
                return 0

            print_report("Eligible before repair:", unresolved_eligible_records(conn))
            result = repair_vote_timestamps(conn, opportunities, apply=args.apply)
            print(f"Vote created_at column present: {result['updated_created_at']}")
            print(f"Vote rows {'updated' if args.apply else 'that would be updated'}: {len(result['repaired'])}")
            if result["missing_votes"]:
                print(f"Missing PURSUE votes: {', '.join(result['missing_votes'])}")
            if result["ineligible_records"]:
                print(f"Skipped ineligible records: {', '.join(result['ineligible_records'])}")
            print_report("Eligible after repair:" if args.apply else "Eligible after dry run would remain:", unresolved_eligible_records(conn))
            if not args.apply:
                print("Dry run only. Re-run with --apply to update local test data.")
        finally:
            conn.close()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
