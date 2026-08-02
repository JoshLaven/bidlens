import json
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity, OpportunityKnowledgeBriefGeneration, OpportunityKnowledgeBriefStatement,
    Organization, OrganizationMembership, User, Workspace,
)
from bidlens.services.opportunity_knowledge_brief.presentation import build_guts_presentation
from bidlens.services.opportunity_knowledge_brief.repository import (
    create_pending_generation, get_latest_successful_generation, mark_generation_running,
    save_generation_success,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "guts"
FIXTURE_PATHS = (
    FIXTURE_ROOT / "v1_communications_heavy.json",
    FIXTURE_ROOT / "v1_notes_and_communications.json",
)


def _parse_datetimes(source):
    return {
        key: datetime.fromisoformat(value) if key in {
            "occurred_at", "effective_at", "updated_at_source",
        } and value is not None else value
        for key, value in source.items()
    }


def _canonical_output(fixture):
    statements = fixture["statements"]
    sections = {}
    for statement in statements:
        if statement["placement_type"] == "section":
            sections.setdefault(statement["section_type"], []).append(statement)
    return {
        "output_schema_version": fixture["generation"]["output_schema_version"],
        "briefing": {
            "headline": next(item for item in statements if item["placement_type"] == "headline"),
            "summary": [item for item in statements if item["placement_type"] == "summary"],
            "sections": [
                {"section_type": section_type, "statements": items}
                for section_type, items in sections.items()
            ],
        },
    }


class GUTSV1CompatibilityFixtureTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.organization = Organization(name="Synthetic V1 Org", slug="synthetic-v1-org")
        self.db.add(self.organization)
        self.db.flush()
        self.workspace = Workspace(
            organization_id=self.organization.id, name="Synthetic V1", slug="synthetic-v1",
        )
        self.user = User(email="reader@example.test", organization_id=self.organization.id)
        self.db.add_all([self.workspace, self.user])
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.organization.id, user_id=self.user.id, role="member",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _load_fixture(self, path, index):
        fixture = json.loads(path.read_text())
        opportunity = Opportunity(
            organization_id=self.organization.id,
            source="synthetic_fixture",
            source_record_id=f"V1-{index}",
            solicitation_number=f"V1-{index}",
            title=fixture["current_state_snapshot"]["title"],
            agency="Synthetic Agency",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 1),
            response_deadline=date.fromisoformat(
                fixture["current_state_snapshot"]["response_deadline"]
            ),
            qualification_status="qualified",
        )
        self.db.add(opportunity)
        self.db.commit()
        with (
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_PROMPT_VERSION", "guts-v8"),
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_OUTPUT_SCHEMA_VERSION", "guts-output-v1"),
        ):
            generation = create_pending_generation(
                self.db, organization_id=self.organization.id, workspace_id=self.workspace.id,
                opportunity_id=opportunity.id, generated_by_user_id=self.user.id,
            )
        self.assertEqual(generation.prompt_version, fixture["generation"]["prompt_version"])
        self.assertEqual(generation.manifest_version, fixture["generation"]["manifest_version"])
        self.assertEqual(
            generation.output_schema_version, fixture["generation"]["output_schema_version"],
        )
        mark_generation_running(self.db, generation)
        save_generation_success(
            self.db, generation,
            output_json=_canonical_output(fixture),
            current_state_snapshot_json=fixture["current_state_snapshot"],
            sources=[_parse_datetimes(source) for source in fixture["sources"]],
            statements=fixture["statements"],
            reproducibility_status=fixture["generation"]["reproducibility_status"],
            metadata={
                "manifest_hash": fixture["generation"]["manifest_hash"],
                "provider": fixture["generation"]["provider"],
                "model": fixture["generation"]["model"],
                "statistics_json": {"fixture_case": fixture["case"]},
            },
        )
        loaded = get_latest_successful_generation(
            self.db, organization_id=self.organization.id, opportunity_id=opportunity.id,
        )
        return fixture, loaded

    def test_synthetic_v1_generations_load_with_metadata_and_without_attribution(self):
        self.assertIn(
            "attribution_json", OpportunityKnowledgeBriefStatement.__table__.columns.keys(),
        )
        for index, path in enumerate(FIXTURE_PATHS, start=1):
            with self.subTest(path=path.name):
                fixture, generation = self._load_fixture(path, index)
                self.assertEqual(generation.status, "succeeded")
                self.assertEqual(generation.prompt_version, "guts-v8")
                self.assertEqual(generation.output_schema_version, "guts-output-v1")
                self.assertEqual(generation.manifest_version, "guts-manifest-v1")
                self.assertEqual(generation.manifest_hash, fixture["generation"]["manifest_hash"])
                self.assertFalse(any(
                    "attribution" in statement for statement in fixture["statements"]
                ))
                self.assertTrue(all(
                    statement.attribution_json is None for statement in generation.statements
                ))

    def test_v1_presentation_preserves_internal_activity_text_order_and_metadata(self):
        expected = {
            "communications_heavy": ["org-1", "org-2"],
            "notes_and_communications": ["org-note-1", "org-email-1"],
        }
        for index, path in enumerate(FIXTURE_PATHS, start=10):
            with self.subTest(path=path.name):
                fixture, generation = self._load_fixture(path, index)
                presentation = build_guts_presentation(generation)
                activity = presentation.internal_activity
                self.assertEqual(
                    [item.statement_key for item in activity], expected[fixture["case"]],
                )
                canonical = {
                    statement["statement_key"]: statement for statement in fixture["statements"]
                }
                for item in activity:
                    source = canonical[item.statement_key]
                    self.assertEqual(item.text, source["text"])
                    self.assertEqual(item.confidence, source["confidence"])
                    self.assertEqual(item.importance, source["importance"])
                    self.assertEqual(
                        [citation.source_id for citation in item.citations], source["source_ids"],
                    )
                    self.assertIsNone(item.attribution)

    def test_v1_citations_remain_attached_in_canonical_order(self):
        fixture, generation = self._load_fixture(FIXTURE_PATHS[0], 20)
        expected_statement = next(
            statement for statement in fixture["statements"]
            if statement["statement_key"] == "org-2"
        )
        persisted = next(
            statement for statement in generation.statements
            if statement.statement_key == "org-2"
        )
        self.assertEqual(
            [link.brief_source.source_id for link in persisted.source_links],
            expected_statement["source_ids"],
        )


if __name__ == "__main__":
    unittest.main()
