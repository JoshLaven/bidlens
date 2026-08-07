import asyncio
import csv
import datetime as dt
import io
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Opportunity, Organization, User, Vote
from bidlens.routes import imports
from bidlens.services.market_activity import (
    MarketActivityFilters,
    _merged_account_rows,
    build_market_activity,
    conversion_percent,
    market_period_dates,
)


class MarketActivityAggregationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Market Org", slug="market-org")
        self.other_org = Organization(name="Other Org", slug="other-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.user = User(email="admin@market.test", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _opportunity(self, record_id, **overrides):
        values = {
            "organization_id": self.org.id,
            "source": "sam.gov",
            "source_record_id": record_id,
            "title": f"Opportunity {record_id}",
            "agency": "Agency A",
            "opportunity_type": "Solicitation",
            "posted_date": dt.date(2026, 1, 1),
            "response_deadline": dt.date(2026, 5, 1),
            "qualification_status": "unreviewed",
            "decision_state": "INBOX",
            "account_type": "Federal",
            "naics": "541611",
            "naics_title": "Administrative Management Consulting Services",
            "created_at": dt.datetime(2026, 2, 1),
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _filters(self, start=dt.date(2026, 1, 1), end=dt.date(2026, 3, 31)):
        return MarketActivityFilters(start_date=start, end_date=end)

    def _seed_funnel(self):
        a_shortlisted = self._opportunity("a-short", qualification_status="qualified")
        self._opportunity("a-qualified", qualification_status="qualified")
        self._opportunity("a-imported")
        self._opportunity(
            "b-short",
            agency="Agency B",
            account_type="State Government",
            naics="541690",
            naics_title="Other Scientific Consulting Services",
            decision_state="SHORTLISTED",
        )
        self._opportunity(
            "b-imported",
            agency="Agency B",
            account_type="State Government",
            naics="541690",
            naics_title="Other Scientific Consulting Services",
        )
        self._opportunity("unassigned", agency=" ", account_type=None, naics=None, naics_title=None)
        self._opportunity(
            "outside-period",
            created_at=dt.datetime(2025, 12, 31),
            qualification_status="qualified",
        )
        self._opportunity(
            "other-tenant",
            organization_id=self.other_org.id,
            qualification_status="qualified",
            decision_state="SHORTLISTED",
        )
        self.db.add(Vote(org_id=self.org.id, opp_id=a_shortlisted.id, user_id=self.user.id, vote="PURSUE"))
        self.db.commit()

    def _build(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "filters": self._filters(),
            "view_by": "account",
            "metric": "count",
        }
        values.update(overrides)
        return build_market_activity(self.db, **values)

    def test_time_period_ranges_and_created_at_filtering(self):
        self.assertEqual(market_period_dates("30_days", today=dt.date(2026, 7, 29)), (dt.date(2026, 6, 30), dt.date(2026, 7, 29)))
        self.assertEqual(market_period_dates("90_days", today=dt.date(2026, 7, 29)), (dt.date(2026, 5, 1), dt.date(2026, 7, 29)))
        self.assertEqual(market_period_dates("year_to_date", today=dt.date(2026, 7, 29)), (dt.date(2026, 1, 1), dt.date(2026, 7, 29)))
        self.assertEqual(market_period_dates("1_year", today=dt.date(2026, 7, 29)), (dt.date(2025, 7, 30), dt.date(2026, 7, 29)))
        self._seed_funnel()
        january = self._build(filters=self._filters(dt.date(2026, 1, 1), dt.date(2026, 1, 31)))
        february = self._build(filters=self._filters(dt.date(2026, 2, 1), dt.date(2026, 2, 28)))
        self.assertEqual(january["metrics"]["imported"], 0)
        self.assertEqual(february["metrics"]["imported"], 6)

    def test_count_totals_groupings_missing_rows_and_naics_descriptions(self):
        self._seed_funnel()
        account = self._build(view_by="account")
        account_type = self._build(view_by="account_type")
        naics = self._build(view_by="naics")

        self.assertEqual(account["metrics"], {"imported": 6, "qualified": 3, "shortlisted": 2})
        self.assertEqual(account_type["metrics"], account["metrics"])
        self.assertEqual(naics["metrics"], account["metrics"])
        self.assertEqual(
            {row["label"] for row in account["rows"]},
            {"Agency A", "Agency B", "Unassigned"},
        )
        self.assertIn("No Account Type", {row["label"] for row in account_type["rows"]})
        self.assertIn("No NAICS", {row["label"] for row in naics["rows"]})
        self.assertIn(
            "541611 — Administrative Management Consulting Services",
            {row["label"] for row in naics["rows"]},
        )

    def test_conversion_calculations_and_zero_denominators(self):
        self._seed_funnel()
        result = self._build(metric="conversion")
        rows = {row["label"]: row for row in result["rows"]}

        self.assertEqual(result["metric_conversion"], {"imported": 100.0, "qualified": 50.0, "shortlisted": 66.7})
        self.assertEqual(rows["Agency A"]["conversion"], {"imported": 100.0, "qualified": 66.7, "shortlisted": 50.0})
        self.assertEqual(rows["Agency B"]["conversion"], {"imported": 100.0, "qualified": 50.0, "shortlisted": 100.0})
        self.assertEqual(rows["Unassigned"]["conversion"], {"imported": 100.0, "qualified": 0.0, "shortlisted": None})
        self.assertIsNone(conversion_percent(1, 0))
        empty = self._build(filters=self._filters(dt.date(2024, 1, 1), dt.date(2024, 1, 2)), metric="conversion")
        self.assertEqual(empty["metric_conversion"], {"imported": None, "qualified": None, "shortlisted": None})

    def test_account_aliases_merge_counts_and_recalculate_conversion(self):
        first_raw = "Administration for Children & Families - ACYF/FYSB"
        second_raw = "Administration for Children and Families - ACYF/CB"
        self._opportunity("alias-imported", agency=first_raw)
        self._opportunity("alias-qualified", agency=first_raw, qualification_status="qualified")
        self._opportunity(
            "alias-shortlisted",
            agency=second_raw,
            qualification_status="qualified",
            decision_state="SHORTLISTED",
        )
        self.db.commit()

        result = self._build(metric="conversion", sort="dimension", direction="asc")

        self.assertEqual(result["total_rows"], 1)
        row = result["rows"][0]
        self.assertEqual(row["label"], "Administration for Children and Families")
        self.assertEqual(
            (row["imported"], row["qualified"], row["shortlisted"]),
            (3, 2, 1),
        )
        self.assertEqual(
            row["conversion"],
            {"imported": 100.0, "qualified": 66.7, "shortlisted": 50.0},
        )
        self.assertEqual(set(row["source_accounts"]), {first_raw, second_raw})
        self.assertEqual(
            {opportunity.agency for opportunity in self.db.query(Opportunity).all()},
            {first_raw, second_raw},
        )

    def test_formatting_variants_merge_and_unmatched_values_use_legacy_display(self):
        self._opportunity("format-one", agency="Example & Research")
        self._opportunity("format-two", agency=" example and research ")
        self._opportunity("fallback-one", agency="department.of.nih")
        self._opportunity("fallback-two", agency="DEPARTMENT OF NIH")
        self._opportunity("blank-one", agency=" ")
        self.db.commit()

        rows = {
            row["label"]: row
            for row in self._build(sort="dimension", direction="asc")["rows"]
        }

        self.assertEqual(rows["Example & Research"]["imported"], 2)
        self.assertEqual(rows["Department Of NIH"]["imported"], 2)
        self.assertEqual(rows["Unassigned"]["imported"], 1)

        null_rows = _merged_account_rows([
            SimpleNamespace(dimension=None, imported=1, qualified=0, shortlisted=0),
            SimpleNamespace(dimension="Unassigned", imported=2, qualified=1, shortlisted=0),
        ])
        self.assertEqual(len(null_rows), 1)
        self.assertEqual(null_rows[0]["label"], "Unassigned")
        self.assertEqual(null_rows[0]["imported"], 3)

    def test_account_sort_and_metric_sort_use_merged_rows(self):
        self._opportunity("z-one", agency="Administration for Children & Families - ACYF/FYSB")
        self._opportunity("z-two", agency="Administration for Children and Families - ACYF/CB")
        self._opportunity("alpha", agency="AAA Independent")
        self._opportunity("zulu", agency="Zulu Independent")
        self.db.commit()

        alphabetical = self._build(sort="dimension", direction="asc")["rows"]
        by_imported = self._build(sort="imported", direction="desc")["rows"]

        self.assertEqual(
            [row["label"] for row in alphabetical],
            ["Aaa Independent", "Administration for Children and Families", "Zulu Independent"],
        )
        self.assertEqual(by_imported[0]["label"], "Administration for Children and Families")
        self.assertEqual(by_imported[0]["imported"], 2)

    def test_account_pagination_occurs_after_alias_merge(self):
        alias_label = "Administration for Children and Families"
        self._opportunity("alias-page-one", agency="Administration for Children & Families - ACYF/FYSB")
        self._opportunity("alias-page-two", agency="Administration for Children and Families - ACYF/CB")
        for index in range(10):
            self._opportunity(f"page-{index}", agency=f"Page Account {index:02d}")
        self.db.commit()

        first = self._build(sort="dimension", direction="asc", page=1, page_size=5)
        second = self._build(sort="dimension", direction="asc", page=2, page_size=5)
        third = self._build(sort="dimension", direction="asc", page=3, page_size=5)
        all_labels = [row["label"] for result in (first, second, third) for row in result["rows"]]

        self.assertEqual(first["total_rows"], 11)
        self.assertEqual(first["total_pages"], 3)
        self.assertEqual(all_labels.count(alias_label), 1)
        self.assertEqual(len(all_labels), 11)

    def test_all_columns_sort_ascending_and_descending_in_both_metrics(self):
        self._seed_funnel()
        for metric in ("count", "conversion"):
            for column in ("dimension", "imported", "qualified", "shortlisted"):
                with self.subTest(metric=metric, column=column):
                    asc = self._build(metric=metric, sort=column, direction="asc")["rows"]
                    desc = self._build(metric=metric, sort=column, direction="desc")["rows"]
                    if column == "dimension":
                        asc_values = [row["label"].casefold() for row in asc]
                        desc_values = [row["label"].casefold() for row in desc]
                    elif metric == "conversion":
                        asc_values = [-1 if row["conversion"][column] is None else row["conversion"][column] for row in asc]
                        desc_values = [-1 if row["conversion"][column] is None else row["conversion"][column] for row in desc]
                    else:
                        asc_values = [row[column] for row in asc]
                        desc_values = [row[column] for row in desc]
                    self.assertEqual(asc_values, sorted(asc_values))
                    self.assertEqual(desc_values, sorted(desc_values, reverse=True))

    def test_defaults_and_pagination(self):
        for index in range(23):
            self._opportunity(f"row-{index}", agency=f"Agency {index:02d}")
        self.db.commit()
        count = self._build(page=2)
        conversion = self._build(metric="conversion")
        self.assertEqual((count["sort"], count["direction"]), ("imported", "desc"))
        self.assertEqual(conversion["sort"], "qualified")
        self.assertEqual((count["total_rows"], count["total_pages"], count["page"], len(count["rows"])), (23, 3, 2, 10))

    def test_actual_route_renders_selected_controls_conversions_and_table(self):
        self._seed_funnel()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/market-activity",
            "headers": [],
            "query_string": b"period=1_year&view_by=naics&metric=conversion&sort=shortlisted&direction=asc",
            "server": ("testserver", 80),
            "scheme": "http",
        })
        user = SimpleNamespace(
            id=self.user.id,
            name="Admin User",
            email=self.user.email,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
            current_organization_name=self.org.name,
            current_organization_is_live=True,
            current_role="admin",
            is_platform_admin=False,
        )
        with patch("bidlens.routes.imports.require_admin", return_value=user), patch(
            "bidlens.routes.imports.get_sidebar", return_value={}
        ):
            response = asyncio.run(imports.market_activity_page(request, db=self.db))
        html = response.body.decode()
        self.assertIn('<option value="1_year" selected>Last 1 Year</option>', html)
        self.assertIn('<option value="naics" selected>NAICS</option>', html)
        self.assertNotIn('<option value="account_type">', html)
        self.assertNotIn('>Account Type</option>', html)
        self.assertIn("57.1%", html)
        self.assertIn("No NAICS", html)
        self.assertIn("541611 — Administrative Management Consulting Services", html)
        self.assertIn("Export CSV", html)
        self.assertIn("market-activity/export.csv", html)
        self.assertIn("insights-metric--imported", html)
        self.assertIn("insights-sort-indicator", html)
        self.assertIn("data-insights-sort-link", html)
        self.assertIn("data-insights-filter-select", html)
        self.assertIn("data-insights-filter-link", html)
        self.assertIn("event.preventDefault()", html)
        self.assertIn("window.scrollTo(scrollX, scrollY)", html)
        self.assertNotIn("onchange=\"this.form.submit()\"", html)
        self.assertNotIn("Pushed to Salesforce", html)

    def test_account_type_is_not_a_user_facing_insights_pivot(self):
        self._seed_funnel()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/market-activity",
            "headers": [],
            "query_string": b"view_by=account_type",
            "server": ("testserver", 80),
            "scheme": "http",
        })
        user = SimpleNamespace(
            id=self.user.id,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
            current_role="admin",
        )
        with patch("bidlens.routes.imports.require_admin", return_value=user), patch(
            "bidlens.routes.imports.get_sidebar", return_value={}
        ):
            response = asyncio.run(imports.market_activity_page(request, db=self.db))
        html = response.body.decode()
        self.assertIn('<option value="account" selected>Account</option>', html)
        self.assertNotIn('<option value="account_type">', html)
        self.assertEqual(response.context["dashboard"]["view_by"], "account")

    def test_csv_export_respects_grouping_metric_and_sort_without_pagination(self):
        self._seed_funnel()
        for index in range(12):
            self._opportunity(
                f"export-{index}",
                agency=f"Export Agency {index:02d}",
                account_type=f"Export Type {index:02d}",
                naics=f"77{index:04d}",
                naics_title=None,
            )
        self.db.commit()
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/market-activity/export.csv",
            "headers": [],
            "query_string": b"period=1_year&view_by=account&metric=conversion&sort=dimension&direction=asc",
            "server": ("testserver", 80),
            "scheme": "http",
        })
        user = SimpleNamespace(
            id=self.user.id,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
            current_role="admin",
        )
        with patch("bidlens.routes.imports.require_admin", return_value=user):
            response = asyncio.run(imports.market_activity_export(request, db=self.db))
        rows = list(csv.reader(io.StringIO(response.body.decode())))
        self.assertEqual(rows[0], ["Account", "Imported", "Qualified", "Shortlisted"])
        self.assertEqual(rows[1][0], "Agency A")
        self.assertEqual(len(rows), 16)
        self.assertTrue(all(value.endswith("%") or value == "—" for row in rows[1:] for value in row[1:]))
        self.assertIn("bidlens-insights-account-1_year.csv", response.headers["content-disposition"])
        self.assertEqual(response.headers["cache-control"], "private, no-store")

    def test_csv_export_matches_alias_merged_ui_rows(self):
        self._opportunity(
            "csv-alias-one",
            agency="Administration for Children & Families - ACYF/FYSB",
            qualification_status="qualified",
        )
        self._opportunity(
            "csv-alias-two",
            agency="Administration for Children and Families - ACYF/CB",
            qualification_status="qualified",
            decision_state="SHORTLISTED",
        )
        self.db.commit()
        dashboard = self._build(sort="dimension", direction="asc")
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/admin/market-activity/export.csv",
            "headers": [],
            "query_string": b"period=1_year&view_by=account&metric=count&sort=dimension&direction=asc",
            "server": ("testserver", 80),
            "scheme": "http",
        })
        user = SimpleNamespace(
            id=self.user.id,
            organization_id=self.org.id,
            current_organization_id=self.org.id,
            current_role="admin",
        )

        with patch("bidlens.routes.imports.require_admin", return_value=user):
            response = asyncio.run(imports.market_activity_export(request, db=self.db))

        csv_rows = list(csv.reader(io.StringIO(response.body.decode())))
        self.assertEqual(len(dashboard["rows"]), 1)
        self.assertEqual(csv_rows[1], [dashboard["rows"][0]["label"], "2", "2", "1"])


class MarketActivityTemplateTests(unittest.TestCase):
    def test_table_first_controls_metric_toggle_and_no_salesforce_kpi(self):
        source = Path("src/bidlens/templates/market_activity.html").read_text()
        self.assertIn("Time Period", source)
        self.assertIn("View By", source)
        self.assertIn("Metric", source)
        self.assertIn("insights-segmented-control", source)
        self.assertIn("insights-export", source)
        self.assertIn("insights-table", source)
        self.assertIn("data-insights-sort-link", source)
        self.assertIn("data-insights-filter-select", source)
        self.assertIn("data-insights-filter-link", source)
        self.assertIn("filterForm?.addEventListener('change'", source)
        self.assertIn("event.preventDefault()", source)
        self.assertIn("await fetch(url", source)
        self.assertIn("replaceChildren(...nextTableContent", source)
        self.assertIn("window.history.replaceState", source)
        self.assertIn("window.scrollTo(scrollX, scrollY)", source)
        self.assertNotIn("window.location", source)
        self.assertNotIn("this.form.submit()", source)
        self.assertNotIn("Pushed to Salesforce", source)
        self.assertNotIn("market-view-tabs", source)
        self.assertNotIn("View as chart", source)


class MarketActivityRouteAccessTests(unittest.TestCase):
    def test_member_cannot_access_analytics_page(self):
        member = SimpleNamespace(id=1, organization_id=7, current_organization_id=7, current_role="member")
        request = SimpleNamespace(query_params={}, url=SimpleNamespace(query=""))
        with patch("bidlens.routes.imports.get_current_user", return_value=member), patch(
            "bidlens.routes.imports.attach_request_user_context", return_value=member
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(imports.market_activity_page(request, db=MagicMock()))
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "Only Workspace Admins can view Analytics.")


if __name__ == "__main__":
    unittest.main()
