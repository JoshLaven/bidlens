import unittest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity
from bidlens.routes.opportunities import (
    _apply_stage_filter,
    _apply_type_tab,
    _exclude_inactive_govwin_stages,
    _normalized_opportunity_type,
)


class OpportunityTypeNormalizationTests(unittest.TestCase):
    def test_normalizes_source_specific_types_for_feed_display(self):
        cases = [
            ("govwin_export", "Forecast", "Forecast Pre-RFP", "Forecast"),
            ("govwin_export", "RFI", "Pre-RFP", "RFI"),
            ("govwin_export", "RFP", "Post-RFP", "RFP"),
            ("govwin_export", "Solicitation", None, "RFP"),
            ("sam", "Combined Synopsis/Solicitation", None, "RFP"),
            ("sam", "Sources Sought", None, "RFI"),
            ("sam", "Special Notice", None, "RFI"),
            ("sam", "Presolicitation", None, "RFI"),
            ("grants_gov", "Funding Opportunity", None, "RFP"),
            ("sam", "Unmapped Notice", None, "RFP"),
        ]

        for source, raw_type, source_stage, expected in cases:
            with self.subTest(source=source, raw_type=raw_type, source_stage=source_stage):
                opportunity = Opportunity(
                    source=source,
                    opportunity_type=raw_type,
                    source_stage=source_stage,
                )
                self.assertEqual(
                    _normalized_opportunity_type(opportunity),
                    expected,
                )
                self.assertEqual(opportunity.opportunity_type, raw_type)
                self.assertEqual(opportunity.source_stage, source_stage)

    def test_stage_filter_and_source_selection_exclusion(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            cases = [
                ("forecast", "govwin_export", "Forecast", "Forecast Pre-RFP"),
                ("rfi", "govwin_export", "RFI", "Pre-RFP"),
                ("rfp", "govwin_export", "RFP", "Post-RFP"),
                ("sam-rfi", "sam", "Sources Sought", None),
                ("sam-rfp", "sam", "Solicitation", None),
                ("excluded", "govwin_export", "Source Selection", "Source Selection"),
            ]
            for index, (record_id, source, raw_type, source_stage) in enumerate(cases, start=1):
                db.add(
                    Opportunity(
                        organization_id=1,
                        source=source,
                        source_record_id=record_id,
                        title=record_id,
                        agency="Test Agency",
                        opportunity_type=raw_type,
                        source_stage=source_stage,
                        posted_date=date.today(),
                        response_deadline=date.today() + timedelta(days=30),
                    )
                )
            db.commit()

            base = _exclude_inactive_govwin_stages(db.query(Opportunity))
            self.assertEqual(
                {row.source_record_id for row in _apply_stage_filter(base, "Forecast").all()},
                {"forecast"},
            )
            self.assertEqual(
                {row.source_record_id for row in _apply_stage_filter(base, "RFI").all()},
                {"rfi", "sam-rfi"},
            )
            self.assertEqual(
                {
                    row.source_record_id
                    for row in _apply_stage_filter(base, "Forecast,RFI").all()
                },
                {"forecast", "rfi", "sam-rfi"},
            )
            self.assertEqual(
                {row.source_record_id for row in _apply_stage_filter(base, "RFP").all()},
                {"rfp", "sam-rfp"},
            )
            self.assertNotIn(
                "excluded",
                {row.source_record_id for row in _apply_stage_filter(base, "All").all()},
            )
        finally:
            db.close()
            engine.dispose()

    def test_feed_tabs_use_the_same_two_category_mapping(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            raw_types = [
                "Solicitation",
                "Grant",
                "Unmapped Notice",
                "Sources Sought",
                "Special Notice",
            ]
            for index, raw_type in enumerate(raw_types, start=1):
                db.add(
                    Opportunity(
                        organization_id=1,
                        source="test",
                        source_record_id=f"type-{index}",
                        title=f"Opportunity {index}",
                        agency="Test Agency",
                        opportunity_type=raw_type,
                        posted_date=date.today(),
                        response_deadline=date.today() + timedelta(days=30),
                    )
                )
            db.commit()

            solicitations = {
                opportunity.opportunity_type
                for opportunity in _apply_type_tab(
                    db.query(Opportunity),
                    "solicitations",
                ).all()
            }
            rfis = {
                opportunity.opportunity_type
                for opportunity in _apply_type_tab(
                    db.query(Opportunity),
                    "rfi",
                ).all()
            }

            self.assertEqual(
                solicitations,
                {"Solicitation", "Grant", "Unmapped Notice"},
            )
            self.assertEqual(rfis, {"Sources Sought", "Special Notice"})
        finally:
            db.close()
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
