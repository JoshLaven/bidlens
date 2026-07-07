import asyncio
import datetime as dt
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from bidlens.database import Base
from bidlens.ingest_sam import (
    ALLOWED_TYPES,
    ingest_sam,
    normalize_sam_record,
    pull_sam_into_db,
)
from bidlens.models import (
    IngestionRun,
    Opportunity,
    Organization,
    OrganizationMembership,
    SamSourceConfig,
    User,
)
from bidlens.routes import imports, sam
from bidlens.sam_client import SamRateLimitError
from bidlens.services.sam_source_config import (
    SamConfigValidationError,
    ingest_kwargs,
    naics_catalog,
    validate_sam_config_input,
)


class SamSourceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="SAM Config Org", slug="sam-config-org")
        self.other_org = Organization(name="Other Org", slug="sam-other-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.admin = User(email="sam-admin@example.com", organization_id=self.org.id)
        self.member = User(email="sam-member@example.com", organization_id=self.org.id)
        self.db.add_all([self.admin, self.member])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.admin.id,
                role="admin",
            ),
            OrganizationMembership(
                organization_id=self.org.id,
                user_id=self.member.id,
                role="member",
            ),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _request(path="/admin/sources/sam", query_string=b""):
        return Request({
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": query_string,
            "headers": [],
        })

    def _config(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "name": "Federal health",
            "naics_codes": ["541611", "541690"],
            "keywords": ["health"],
            "agencies": ["HHS"],
            "set_asides": ["SBA"],
            "notice_types": ["Solicitation"],
            "posted_days_back": 30,
            "due_days_from": 5,
            "due_days_to": 60,
            "active_only": True,
            "max_records": 50,
        }
        values.update(overrides)
        config = SamSourceConfig(**values)
        self.db.add(config)
        self.db.commit()
        return config

    def test_validation_normalizes_values_and_rejects_invalid_windows(self):
        values = validate_sam_config_input(
            naics_codes="541611, 541690\n541611",
            keywords="health, Medicaid",
            agencies="HHS\nCMS",
            set_asides="SBA",
            notice_types=["Solicitation", "Sources Sought"],
            posted_days_back="30",
            due_days_from="5",
            due_days_to="90",
            active_only=True,
            max_records="250",
        )

        self.assertEqual(values["naics_codes"], ["541611", "541690"])
        self.assertEqual(values["keywords"], ["health", "Medicaid"])
        self.assertEqual(values["max_records"], 250)

        with self.assertRaises(SamConfigValidationError) as context:
            validate_sam_config_input(
                naics_codes="54A611",
                keywords="",
                agencies="",
                set_asides="",
                notice_types=[],
                posted_days_back="400",
                due_days_from="90",
                due_days_to="10",
                active_only=False,
                max_records="5000",
            )
        self.assertEqual(
            set(context.exception.errors),
            {"naics_codes", "posted_days_back", "due_days_to", "max_records"},
        )

    @patch("bidlens.ingest_sam.search_opportunities")
    def test_saved_criteria_filter_records_before_upsert(self, search_opportunities):
        today = dt.date.today()

        def record(record_id, **overrides):
            values = {
                "noticeId": record_id,
                "title": "Behavioral health evaluation",
                "department": "HHS",
                "type": "Solicitation",
                "postedDate": today.isoformat(),
                "responseDeadLine": (today + dt.timedelta(days=30)).isoformat(),
                "uiLink": f"https://sam.gov/opp/{record_id}",
                "naicsCode": "541611",
                "typeOfSetAside": "SBA",
                "active": "Yes",
            }
            values.update(overrides)
            return values

        search_opportunities.side_effect = [
            {
                "opportunitiesData": [
                    record("match"),
                    record("wrong-keyword", title="Office furniture"),
                    record("inactive", active="No"),
                ]
            },
            {"opportunitiesData": []},
        ]

        result = pull_sam_into_db(
            self.db,
            organization_id=self.org.id,
            naics="541611",
            days_back=30,
            allowed_types={"Solicitation"},
            keywords={"health"},
            agencies={"HHS"},
            set_asides={"SBA"},
            due_days_from=5,
            due_days_to=60,
            active_only=True,
            max_records=10,
            limit=10,
            organization_name="HHS",
        )

        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["filtered"], 2)
        self.assertEqual(result["records_seen"], 3)
        first_call = search_opportunities.call_args_list[0].kwargs
        self.assertEqual(first_call["response_deadline_from"], today + dt.timedelta(days=5))
        self.assertEqual(first_call["response_deadline_to"], today + dt.timedelta(days=60))
        self.assertEqual(first_call["organization_name"], "HHS")
        self.assertEqual(first_call["procurement_types"], ["o"])

    @patch("bidlens.ingest_sam.search_opportunities")
    def test_default_request_excludes_awards_and_fallback_reason_is_specific(
        self,
        search_opportunities,
    ):
        today = dt.date.today()

        def record(record_id, notice_type):
            return {
                "noticeId": record_id,
                "title": f"{notice_type} opportunity",
                "department": "HHS",
                "type": notice_type,
                "postedDate": today.isoformat(),
                "responseDeadLine": (today + dt.timedelta(days=30)).isoformat(),
                "uiLink": f"https://sam.gov/opp/{record_id}",
                "naicsCode": "541611",
                "active": "Yes",
            }

        search_opportunities.side_effect = [
            {
                "opportunitiesData": [
                    record("award-1", "Award Notice"),
                    record("solicitation-1", "Solicitation"),
                ]
            },
            {"opportunitiesData": []},
        ]

        result = pull_sam_into_db(
            self.db,
            organization_id=self.org.id,
            naics="541611",
            allowed_types=set(),
            max_records=10,
            limit=10,
        )

        first_call = search_opportunities.call_args_list[0].kwargs
        self.assertEqual(
            first_call["procurement_types"],
            ["k", "o", "p", "r", "s"],
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["filtered"], 1)
        self.assertEqual(
            result["_record_details"][0]["reason"],
            "Award Notice excluded from V1 discovery imports",
        )
        self.assertEqual(
            self.db.query(Opportunity).filter(
                Opportunity.source_record_id == "award-1"
            ).count(),
            0,
        )

    @patch("bidlens.ingest_sam.search_opportunities")
    def test_total_records_avoids_trailing_empty_page_probe(self, search_opportunities):
        today = dt.date.today()
        search_opportunities.return_value = {
            "totalRecords": 1,
            "opportunitiesData": [{
                "noticeId": "single-page",
                "title": "Single page opportunity",
                "department": "HHS",
                "type": "Solicitation",
                "postedDate": today.isoformat(),
                "responseDeadLine": (today + dt.timedelta(days=30)).isoformat(),
                "uiLink": "https://sam.gov/opp/single-page",
            }],
        }

        result = pull_sam_into_db(
            self.db,
            organization_id=self.org.id,
            naics="541611",
            limit=100,
        )

        self.assertEqual(result["records_seen"], 1)
        self.assertEqual(result["search_requests_made"], 1)
        self.assertEqual(search_opportunities.call_count, 1)

    @patch("bidlens.ingest_sam.search_opportunities")
    def test_rate_limit_returns_paused_checkpoint_without_counting_error(
        self,
        search_opportunities,
    ):
        search_opportunities.side_effect = SamRateLimitError(
            "quota exceeded",
            retry_after_seconds=3600,
            retry_after="Sun, 05 Jul 2026 23:00:00 GMT",
        )

        result = pull_sam_into_db(
            self.db,
            organization_id=self.org.id,
            naics="541611",
            start_offset=100,
            initial_pulled=100,
        )

        self.assertTrue(result["paused_rate_limit"])
        self.assertEqual(result["next_offset"], 100)
        self.assertEqual(result["scope_pulled"], 100)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["search_requests_made"], 1)

    def test_supported_discovery_notice_types_still_normalize(self):
        today = dt.date.today()
        for notice_type in ALLOWED_TYPES:
            with self.subTest(notice_type=notice_type):
                record = {
                    "noticeId": f"notice-{notice_type}",
                    "title": f"{notice_type} opportunity",
                    "department": "HHS",
                    "type": notice_type,
                    "postedDate": today.isoformat(),
                    "responseDeadLine": (today + dt.timedelta(days=30)).isoformat(),
                    "uiLink": f"https://sam.gov/opp/{notice_type}",
                }
                normalized = normalize_sam_record(record, set())
                self.assertIsNotNone(normalized)
                self.assertEqual(normalized["opportunity_type"], notice_type)

    @patch("bidlens.ingest_sam.pull_sam_into_db")
    def test_max_records_is_shared_across_configured_naics(self, pull):
        def result(_db, **kwargs):
            count = kwargs["max_records"]
            return {
                "inserted": count,
                "updated": 0,
                "unchanged": 0,
                "skipped": 0,
                "filtered": 0,
                "errors": 0,
                "pages_pulled": 1,
                "records_seen": count,
                "search_requests_made": 1,
                "pulled": count,
                "_record_details": [],
            }

        pull.side_effect = result
        output = ingest_sam(
            self.db,
            organization_id=self.org.id,
            naics_list=["541611", "541690"],
            max_records=5,
        )

        self.assertEqual(output["records_seen"], 5)
        self.assertEqual([call.kwargs["max_records"] for call in pull.call_args_list], [3, 2])

    @patch("bidlens.ingest_sam.pull_sam_into_db")
    def test_paused_saved_search_resumes_same_run_and_keeps_totals(self, pull):
        config = self._config(naics_codes=["541611"], agencies=[])
        pull.return_value = {
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "pages_pulled": 1,
            "records_seen": 1,
            "search_requests_made": 2,
            "pulled": 1,
            "paused_rate_limit": True,
            "next_offset": 100,
            "scope_pulled": 1,
            "retry_after_seconds": 3600,
            "retry_after": None,
            "_record_details": [],
        }
        first = ingest_sam(
            self.db,
            organization_id=self.org.id,
            naics_list=["541611"],
            source_config_id=config.id,
            max_records=5,
            manual_pull=True,
        )

        self.assertEqual(first["status"], "paused_rate_limit")
        run = self.db.get(IngestionRun, first["run_id"])
        self.assertEqual(run.status, "paused_rate_limit")
        self.assertEqual(run.checkpoint_json["offset"], 100)
        self.assertEqual(run.error_count, 0)

        run.retry_after_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        self.db.commit()
        pull.return_value = {
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "pages_pulled": 1,
            "records_seen": 1,
            "search_requests_made": 1,
            "pulled": 2,
            "paused_rate_limit": False,
            "next_offset": 200,
            "scope_pulled": 2,
            "_record_details": [],
        }
        second = ingest_sam(
            self.db,
            organization_id=self.org.id,
            naics_list=["541611"],
            source_config_id=config.id,
            max_records=5,
            manual_pull=True,
        )

        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["inserted"], 2)
        self.assertEqual(pull.call_args.kwargs["start_offset"], 100)
        self.assertEqual(pull.call_args.kwargs["initial_pulled"], 1)
        self.db.refresh(run)
        self.assertIsNone(run.checkpoint_json)
        self.assertIsNotNone(run.finished_at)

    def test_config_save_is_scoped_to_current_workspace(self):
        request = self._request(query_string=f"org_id={self.org.id}".encode())
        setattr(self.admin, "current_organization_id", self.org.id)
        with patch("bidlens.routes.imports.require_admin", return_value=self.admin):
            response = asyncio.run(imports.save_sam_source_config(
                request=request,
                config_id="",
                search_name="Federal health",
                naics_codes="541611\n541690",
                keywords="health",
                agencies="HHS",
                set_asides="SBA",
                notice_types=["Solicitation"],
                posted_days_back="30",
                due_days_from="5",
                due_days_to="60",
                active_only="1",
                max_records="100",
                db=self.db,
            ))

        config = self.db.query(SamSourceConfig).one()
        self.assertEqual(config.organization_id, self.org.id)
        self.assertEqual(config.name, "Federal health")
        self.assertEqual(config.naics_codes, ["541611", "541690"])
        self.assertEqual(response.status_code, 303)

    @patch("bidlens.routes.sam._record_sam_source_activity")
    @patch("bidlens.routes.sam.ingest_sam")
    @patch("bidlens.routes.sam.current_org_id")
    def test_manual_pull_uses_saved_workspace_config(
        self,
        current_org_id,
        ingest,
        record_activity,
    ):
        config = self._config()
        self._config(name="A different search", naics_codes=["611310"], keywords=["education"])
        current_org_id.return_value = self.org.id
        ingest.return_value = {
            "status": "success",
            "run_id": 12,
            "inserted": 0,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "records_seen": 0,
            "pages_pulled": 0,
            "results": [],
        }

        response = sam.pull_now(
            request=self._request(path="/sam/pull-now"),
            search_id=config.id,
            user=self.admin,
            db=self.db,
        )

        self.assertEqual(response.status_code, 200)
        kwargs = ingest.call_args.kwargs
        self.assertEqual(kwargs["organization_id"], self.org.id)
        self.assertEqual(kwargs["naics_list"], config.naics_codes)
        self.assertEqual(kwargs["keywords"], {"health"})
        self.assertEqual(kwargs["agencies"], {"HHS"})
        self.assertEqual(kwargs["max_records"], 50)
        self.assertEqual(kwargs["saved_search_name"], "Federal health")
        self.assertEqual(kwargs["run_type"], "Manual")
        self.assertEqual(kwargs["source_config_id"], config.id)

    def test_multiple_named_searches_are_workspace_scoped(self):
        first = self._config(name="Federal health")
        second = self._config(name="Research services", naics_codes=["541715"])
        other = self._config(
            organization_id=self.other_org.id,
            name="Federal health",
            naics_codes=["611310"],
        )

        rows = (
            self.db.query(SamSourceConfig)
            .filter(SamSourceConfig.organization_id == self.org.id)
            .order_by(SamSourceConfig.name.asc())
            .all()
        )
        self.assertEqual({row.id for row in rows}, {first.id, second.id})
        self.assertNotIn(other.id, {row.id for row in rows})

    def test_naics_catalog_contains_searchable_codes_and_labels(self):
        catalog = naics_catalog()
        consulting = [
            item for item in catalog
            if item["code"] == "541611"
        ]
        self.assertEqual(len(consulting), 1)
        self.assertIn("Consulting", consulting[0]["label"])

    def test_pull_history_records_saved_search_name_and_run_type(self):
        result = {
            "saved_search_name": "Federal health",
            "run_type": "Manual",
            "records_seen": 3,
            "inserted": 1,
            "updated": 0,
            "unchanged": 2,
            "skipped": 0,
            "filtered": 0,
            "errors": 0,
            "message": "Pull completed.",
        }

        sam._record_sam_source_activity(
            self.db,
            org_id=self.org.id,
            user_id=self.admin.id,
            result=result,
        )

        run = self.db.query(IngestionRun).one()
        self.assertEqual(run.filename, "Manual saved search: Federal health")
        self.assertEqual(run.processed_count, 3)

    def test_non_admin_cannot_run_pull(self):
        with (
            patch("bidlens.routes.sam.current_org_id", return_value=self.org.id),
            self.assertRaises(Exception) as context,
        ):
            sam.pull_now(
                request=self._request(path="/sam/pull-now"),
                user=self.member,
                db=self.db,
            )
        self.assertEqual(getattr(context.exception, "status_code", None), 403)

    def test_ingest_kwargs_keeps_source_and_feed_configuration_separate(self):
        config = self._config()
        kwargs = ingest_kwargs(config)

        self.assertEqual(kwargs["days_back"], 30)
        self.assertEqual(kwargs["allowed_types"], {"Solicitation"})
        self.assertNotIn("include_keywords", kwargs)
        self.assertNotIn("min_days_out", kwargs)


if __name__ == "__main__":
    unittest.main()
