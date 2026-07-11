#!/usr/bin/env python3
"""Reset the local BidLens development database.

This utility is intentionally scoped to local development. It clears all
workspace/customer data while preserving exactly one login account:

    joshuatlaven@gmail.com

Because the current legacy User schema still has a non-null organization_id,
this reset creates/reuses a local-only internal BidLens Platform organization
and assigns the preserved Platform Owner user to it. Customer organizations and
customer workspaces are still fully removed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import delete, func, insert, select, text, update


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bidlens.config import DATABASE_URL  # noqa: E402
from bidlens.database import Base, SessionLocal, engine  # noqa: E402
from bidlens.models import User  # noqa: E402,F401 - imported to register metadata
import bidlens.models  # noqa: E402,F401 - ensure all model tables are registered


PLATFORM_OWNER_EMAIL = "joshuatlaven@gmail.com"
PLATFORM_ORG_NAME = "BidLens Platform"
PLATFORM_ORG_SLUG = "bidlens-platform"
PLATFORM_ORG_PLAN = "platform"


def _env_looks_production() -> bool:
    values = [
        os.getenv("ENV"),
        os.getenv("APP_ENV"),
        os.getenv("BIDLENS_ENV"),
        os.getenv("FASTAPI_ENV"),
    ]
    return any(str(value or "").strip().lower() in {"prod", "production"} for value in values)


def _sqlite_path_from_url(database_url: str) -> Path | None:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        return None
    if database_url.startswith("sqlite:///:memory:"):
        return REPO_ROOT / ":memory:"
    if database_url.startswith("sqlite:///"):
        raw_path = database_url.removeprefix("sqlite:///")
        path = Path(raw_path)
        if not path.is_absolute():
            path = (REPO_ROOT / path).resolve()
        return path
    return None


def assert_local_development_database() -> Path:
    if _env_looks_production():
        raise SystemExit("Refusing to reset database: environment looks like production.")

    db_path = _sqlite_path_from_url(DATABASE_URL)
    if db_path is None:
        raise SystemExit(
            "Refusing to reset database: reset_dev.py only supports local SQLite databases. "
            f"DATABASE_URL={DATABASE_URL!r}"
        )

    if db_path.name != ":memory:":
        try:
            db_path.relative_to(REPO_ROOT)
        except ValueError as exc:
            raise SystemExit(
                "Refusing to reset database outside this repository: "
                f"{db_path}"
            ) from exc

    lowered = DATABASE_URL.lower()
    blocked_words = ("prod", "production", "staging", "render.com", "amazonaws", "rds")
    if any(word in lowered for word in blocked_words):
        raise SystemExit(f"Refusing to reset production-looking DATABASE_URL={DATABASE_URL!r}")

    return db_path


def table_counts(session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = session.execute(select(func.count()).select_from(table)).scalar_one()
    return counts


def reset_database() -> None:
    db_path = assert_local_development_database()

    organizations = Base.metadata.tables["organizations"]
    users = Base.metadata.tables["users"]

    with SessionLocal() as session:
        before = table_counts(session)

    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            # SQLite is the only supported target for this dev reset. Temporarily
            # disabling FK checks lets us preserve the Platform Owner user row
            # while removing every organization row in the current legacy schema.
            connection.execute(text("PRAGMA foreign_keys=OFF"))

        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "users":
                connection.execute(delete(table).where(table.c.email != PLATFORM_OWNER_EMAIL))
            elif table.name == "organizations":
                connection.execute(delete(table))
            else:
                connection.execute(delete(table))

        # Local development workaround: the legacy users table still requires
        # users.organization_id even for Platform-only identity records.
        # TODO: Refactor user identity so users are not required to belong
        # directly to an organization. Future model should use users as
        # identity records and memberships for platform/workspace access.
        platform_org = connection.execute(
            select(organizations.c.id).where(organizations.c.slug == PLATFORM_ORG_SLUG)
        ).first()
        if platform_org is None:
            platform_org_id = connection.execute(
                insert(organizations).values(
                    name=PLATFORM_ORG_NAME,
                    slug=PLATFORM_ORG_SLUG,
                    email_domain=None,
                    plan=PLATFORM_ORG_PLAN,
                    is_active=True,
                    is_live=True,
                )
            ).inserted_primary_key[0]
        else:
            platform_org_id = platform_org.id
            connection.execute(
                update(organizations)
                .where(organizations.c.id == platform_org_id)
                .values(
                    name=PLATFORM_ORG_NAME,
                    slug=PLATFORM_ORG_SLUG,
                    email_domain=None,
                    plan=PLATFORM_ORG_PLAN,
                    is_active=True,
                    is_live=True,
                )
            )

        owner = connection.execute(
            select(users.c.id).where(users.c.email == PLATFORM_OWNER_EMAIL)
        ).first()
        if owner is None:
            connection.execute(
                insert(users).values(
                    email=PLATFORM_OWNER_EMAIL,
                    name=None,
                    organization_id=platform_org_id,
                )
            )
        else:
            connection.execute(
                update(users)
                .where(users.c.email == PLATFORM_OWNER_EMAIL)
                .values(name=None, organization_id=platform_org_id)
            )

        if engine.dialect.name == "sqlite":
            connection.execute(text("PRAGMA foreign_keys=ON"))

    with SessionLocal() as session:
        after = table_counts(session)
        owner_count = session.execute(
            select(func.count()).select_from(users).where(users.c.email == PLATFORM_OWNER_EMAIL)
        ).scalar_one()
        total_users = session.execute(select(func.count()).select_from(users)).scalar_one()
        organizations_count = session.execute(select(func.count()).select_from(organizations)).scalar_one()
        platform_org_row = session.execute(
            select(organizations.c.id, organizations.c.name, organizations.c.slug, organizations.c.plan)
            .where(organizations.c.slug == PLATFORM_ORG_SLUG)
        ).first()
        owner_row = session.execute(
            select(users.c.email, users.c.organization_id).where(users.c.email == PLATFORM_OWNER_EMAIL)
        ).first()

    print("BidLens local development database reset complete.")
    print(f"Database: {db_path}")
    print(f"Preserved Platform Owner: {PLATFORM_OWNER_EMAIL}")
    print(f"Users remaining: {total_users} (owner rows: {owner_count})")
    print(f"Organizations remaining: {organizations_count}")
    if platform_org_row:
        print(
            "Internal platform organization: "
            f"{platform_org_row.name} (id={platform_org_row.id}, slug={platform_org_row.slug}, plan={platform_org_row.plan})"
        )
    if owner_row:
        print(f"Platform Owner organization_id: {owner_row.organization_id}")
    print("Rows removed:")
    for table_name in sorted(before):
        removed = before[table_name] - after.get(table_name, 0)
        if removed > 0:
            print(f"  {table_name}: {removed}")


if __name__ == "__main__":
    reset_database()
