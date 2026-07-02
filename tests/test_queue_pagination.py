import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization
from bidlens.routes.opportunities import (
    FEED_PAGE_SIZE,
    _apply_feed_ordering,
    _feed_query,
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


if __name__ == "__main__":
    unittest.main()
