from __future__ import annotations

import csv
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from bidlens.database import SessionLocal
from bidlens.models import Opportunity, Organization, User, Vote


def main() -> int:
    db = SessionLocal()
    try:
        rows = (
            db.query(Organization, User, Opportunity, Vote)
            .join(Vote, Vote.org_id == Organization.id)
            .join(User, User.id == Vote.user_id)
            .join(
                Opportunity,
                (Opportunity.id == Vote.opp_id)
                & (Opportunity.organization_id == Organization.id),
            )
            .filter(
                Vote.vote == "PURSUE",
                Opportunity.salesforce_opportunity_id.is_(None),
            )
            .order_by(Organization.name.asc(), User.email.asc(), Opportunity.id.asc())
            .all()
        )

        writer = csv.writer(sys.stdout)
        writer.writerow([
            "organization_id",
            "organization_name",
            "user_id",
            "user_email",
            "opportunity_id",
            "source",
            "source_record_id",
            "external_source_key",
            "title",
        ])
        for organization, user, opportunity, _vote in rows:
            writer.writerow([
                organization.id,
                organization.name,
                user.id,
                user.email,
                opportunity.id,
                opportunity.source,
                opportunity.source_record_id,
                opportunity.external_source_key,
                opportunity.title,
            ])

        print(f"\nUnlinked Interested records: {len(rows)}", file=sys.stderr)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
