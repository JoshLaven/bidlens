import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bidlens.database import SessionLocal
from src.bidlens.models import Opportunity
from src.bidlens.services.account_type_classifier import classify_account_type


def main() -> None:
    db = SessionLocal()
    updated = 0
    try:
        opportunities = (
            db.query(Opportunity)
            .filter(Opportunity.source == "govwin_export")
            .filter((Opportunity.account_type_source.is_(None)) | (Opportunity.account_type_source != "manual"))
            .all()
        )
        for opportunity in opportunities:
            classification = classify_account_type(opportunity.agency)
            opportunity.account_type = classification.account_type
            opportunity.account_type_confidence = classification.confidence
            opportunity.account_type_source = classification.source
            updated += 1
        db.commit()
    finally:
        db.close()
    print(f"Backfilled account type metadata for {updated} GovWin opportunities")


if __name__ == "__main__":
    main()
