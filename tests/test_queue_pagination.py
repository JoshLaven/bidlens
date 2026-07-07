import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, Vote
from bidlens.routes.opportunities import (
    FEED_PAGE_SIZE,
    _apply_feed_ordering,
    _apply_stage_filter,
    _apply_triage_source_filter,
    _feed_query,
    _my_shortlist_query,
    _normalize_triage_source_filters,
    _pagination_values,
)


class QueuePaginationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Pagination", slug="pagination")
        self.db.add(self.org)
        self.db.flush()
        self.user = SimpleNamespace(
            id=1,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
        )

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, source, source_record_id, upserted_at):
        return Opportunity(
            organization_id=self.org.id,
            source=source,
            source_record_id=source_record_id,
            title=f"{source} {source_record_id}",
            agency="Test Agency",
            opportunity_type="Solicitation",
            posted_date=date(2026, 6, 1),
            response_deadline=date.today() + timedelta(days=30),
            qualification_status="qualified",
            upserted_at=upserted_at,
        )

    def test_newer_source_batch_does_not_make_older_source_unreachable(self):
        older_grant = self._opportunity(
            "grants_gov",
            "grant-1",
            datetime(2026, 6, 1),
        )
        newer_govwin = [
            self._opportunity(
                "govwin_export",
                f"govwin-{index}",
                datetime(2026, 6, 2) + timedelta(seconds=index),
            )
            for index in range(FEED_PAGE_SIZE)
        ]
        self.db.add_all([older_grant, *newer_govwin])
        self.db.commit()

        query = _apply_feed_ordering(
            _feed_query(self.db, self.user, "solicitations"),
            sort="imported",
            direction="desc",
        )
        result_count = query.count()
        page, total_pages, offset = _pagination_values(
            result_count,
            2,
            FEED_PAGE_SIZE,
        )
        second_page = query.offset(offset).limit(FEED_PAGE_SIZE).all()

        self.assertEqual(page, 2)
        self.assertEqual(total_pages, 2)
        self.assertEqual([row[0].source for row in second_page], ["grants_gov"])

    def test_feed_includes_rfi_types_without_a_separate_tab(self):
        solicitation = self._opportunity(
            "sam",
            "solicitation-1",
            datetime(2026, 6, 2),
        )
        rfi = self._opportunity(
            "sam",
            "rfi-1",
            datetime(2026, 6, 3),
        )
        rfi.opportunity_type = "Sources Sought"
        self.db.add_all([solicitation, rfi])
        self.db.commit()

        rows = _feed_query(self.db, self.user, "solicitations").all()

        self.assertEqual(
            {opportunity.id for opportunity, _watched in rows},
            {solicitation.id, rfi.id},
        )

    def test_my_shortlist_uses_feed_stage_filters_instead_of_legacy_tabs(self):
        forecast = self._opportunity(
            "govwin_export",
            "forecast-1",
            datetime(2026, 6, 1),
        )
        forecast.opportunity_type = "Forecast"
        forecast.source_stage = "Forecast Pre-RFP"
        rfi = self._opportunity("sam", "rfi-1", datetime(2026, 6, 2))
        rfi.opportunity_type = "Sources Sought"
        rfp = self._opportunity("sam", "rfp-1", datetime(2026, 6, 3))
        self.db.add_all([forecast, rfi, rfp])
        self.db.flush()
        self.db.add_all([
            Vote(org_id=self.org.id, user_id=self.user.id, opp_id=opp.id, vote="PURSUE")
            for opp in (forecast, rfi, rfp)
        ])
        self.db.commit()

        base = _my_shortlist_query(self.db, self.user, "solicitations")
        self.assertEqual(
            {opp.id for opp, _watched in base.all()},
            {forecast.id, rfi.id, rfp.id},
        )
        filtered = _apply_stage_filter(base, "Forecast,RFI").all()
        self.assertEqual(
            {opp.id for opp, _watched in filtered},
            {forecast.id, rfi.id},
        )

    def test_triage_source_filter_normalizes_sources_and_combines_with_stage(self):
        sam_rfi = self._opportunity("sam", "sam-rfi", datetime(2026, 6, 1))
        sam_rfi.opportunity_type = "Sources Sought"
        govwin_rfi = self._opportunity(
            "govwin_export",
            "govwin-rfi",
            datetime(2026, 6, 2),
        )
        govwin_rfi.opportunity_type = "RFI"
        govwin_rfi.source_stage = "Pre-RFP"
        grant_rfp = self._opportunity(
            "grants_gov",
            "grant-rfp",
            datetime(2026, 6, 3),
        )
        self.db.add_all([sam_rfi, govwin_rfi, grant_rfp])
        self.db.commit()

        base = self.db.query(Opportunity)
        filtered = _apply_stage_filter(
            _apply_triage_source_filter(base, "govwin"),
            "RFI",
        ).all()

        self.assertEqual(
            [opportunity.source_record_id for opportunity in filtered],
            ["govwin-rfi"],
        )
        self.assertEqual(
            _normalize_triage_source_filters("sam.gov,grants_gov,govwin_api"),
            ("sam", "grants", "govwin"),
        )
        self.assertEqual(_apply_triage_source_filter(base, "").count(), 0)
        self.assertEqual(_apply_triage_source_filter(base, None).count(), 3)


if __name__ == "__main__":
    unittest.main()
