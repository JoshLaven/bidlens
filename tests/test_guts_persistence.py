import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityKnowledgeBriefGeneration,
    OpportunityKnowledgeBriefSource,
    OpportunityKnowledgeBriefStatement,
    OpportunityKnowledgeBriefStatementSource,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.services.opportunity_knowledge_brief import (
    ActiveKnowledgeBriefGenerationError,
    FailureCategory,
    GenerationStatus,
    KnowledgeBriefLifecycleError,
    KnowledgeBriefScopeError,
    KnowledgeBriefValidationError,
    ReproducibilityStatus,
    create_pending_generation,
    expire_stale_generation,
    get_active_generation,
    get_latest_successful_generation,
    mark_generation_failed,
    mark_generation_running,
    save_generation_success,
)


class GutsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.org = Organization(name="GUTS Org", slug="guts-org")
        self.other_org = Organization(name="Other Org", slug="guts-other")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="GUTS", slug="guts")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="guts-other")
        self.user = User(email="guts@example.test", organization_id=self.org.id)
        self.other_user = User(email="other-guts@example.test", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.user, self.other_user])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.other_user.id, role="member"),
        ])
        self.opportunity = self._new_opportunity(self.org.id, "GUTS-1")
        self.other_opportunity = self._new_opportunity(self.other_org.id, "GUTS-2")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _new_opportunity(self, organization_id, source_record_id):
        opportunity = Opportunity(
            organization_id=organization_id,
            source="test",
            source_record_id=source_record_id,
            solicitation_number=source_record_id,
            title=source_record_id,
            agency="Agency",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 31),
            response_deadline=date(2026, 9, 1),
            qualification_status="qualified",
        )
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def _pending(self, *, requested_at=None):
        return create_pending_generation(
            self.db,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id,
            generated_by_user_id=self.user.id,
            requested_at=requested_at,
        )

    @staticmethod
    def _sources():
        return [{
            "source_id": "current:1",
            "source_class": "current_state",
            "source_type": "opportunity",
            "authority": "authoritative_current",
            "citation_label": "Current opportunity",
            "internal_model_name": "Opportunity",
            "internal_record_id": "1",
            "retained_by_bidlens": True,
            "selected_character_count": 120,
            "original_character_count": 120,
        }]

    @staticmethod
    def _statements():
        return [{
            "statement_key": "headline-1",
            "placement_type": "headline",
            "section_type": None,
            "section_title": None,
            "position": 0,
            "text": "The response deadline is September 1, 2026.",
            "importance": "high",
            "confidence": "supported",
            "source_ids": ["current:1"],
        }]

    def _succeed(self, generation, *, completed_at=None):
        mark_generation_running(self.db, generation)
        return save_generation_success(
            self.db,
            generation,
            output_json={"statements": [{"key": "headline-1"}]},
            current_state_snapshot_json={"title": "GUTS-1"},
            sources=self._sources(),
            statements=self._statements(),
            reproducibility_status=ReproducibilityStatus.FULLY_REPRODUCIBLE,
            completed_at=completed_at,
            metadata={
                "manifest_hash": "a" * 64,
                "source_summary_json": {"current_state": 1},
                "warning_metadata_json": [],
                "statistics_json": {"statement_count": 1},
                "input_character_count": 120,
                "input_tokens": 30,
                "output_tokens": 12,
                "total_tokens": 42,
                "total_ms": 250,
            },
        )

    def test_pending_defaults_relationships_and_json_fields(self):
        generation = self._pending()
        self.assertEqual(generation.status, GenerationStatus.PENDING)
        self.assertEqual(generation.validation_retry_count, 0)
        self.assertEqual(generation.degraded_source_count, 0)
        self.assertFalse(generation.input_truncated)
        self.assertEqual(generation.opportunity.id, self.opportunity.id)
        self.assertEqual(generation.workspace.id, self.workspace.id)
        self.assertEqual(generation.generated_by_user.id, self.user.id)
        self.assertIsNone(generation.output_json)
        self.assertIsNone(generation.failure_category)

    def test_scope_validation_rejects_mismatches(self):
        with self.assertRaises(KnowledgeBriefScopeError):
            create_pending_generation(
                self.db, organization_id=self.org.id, workspace_id=self.other_workspace.id,
                opportunity_id=self.opportunity.id, generated_by_user_id=self.user.id,
            )
        with self.assertRaises(KnowledgeBriefScopeError):
            create_pending_generation(
                self.db, organization_id=self.org.id, workspace_id=self.workspace.id,
                opportunity_id=self.other_opportunity.id, generated_by_user_id=self.user.id,
            )

    def test_active_generation_lookup_and_single_active_guard(self):
        generation = self._pending()
        self.assertEqual(get_active_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        ).id, generation.id)
        with self.assertRaises(ActiveKnowledgeBriefGenerationError):
            self._pending()

    def test_running_failure_and_final_attempt_protection(self):
        generation = mark_generation_running(self.db, self._pending())
        self.assertEqual(generation.status, GenerationStatus.RUNNING)
        failed = mark_generation_failed(
            self.db, generation,
            failure_category=FailureCategory.MODEL_TIMEOUT,
            failure_stage="model",
            safe_error_message="Provider timed out.",
            metadata={"model_ms": 45000, "input_character_count": 5000},
        )
        self.assertEqual(failed.status, GenerationStatus.FAILED)
        self.assertEqual(failed.failure_category, "model_timeout")
        self.assertEqual(failed.model_ms, 45000)
        with self.assertRaises(KnowledgeBriefLifecycleError):
            mark_generation_running(self.db, failed)
        with self.assertRaises(KnowledgeBriefLifecycleError):
            mark_generation_failed(self.db, failed, failure_category="unexpected_error")

    def test_success_is_atomic_and_eagerly_loads_citations(self):
        generation = self._succeed(self._pending())
        self.assertEqual(generation.status, GenerationStatus.SUCCEEDED)
        self.assertEqual(generation.current_state_snapshot_json, {"title": "GUTS-1"})
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefSource).count(), 1)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatement).count(), 1)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatementSource).count(), 1)
        loaded = get_latest_successful_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        )
        link = loaded.statements[0].source_links[0]
        self.assertEqual(link.brief_source.source_id, "current:1")
        with self.assertRaises(KnowledgeBriefLifecycleError):
            save_generation_success(
                self.db, generation, output_json={}, current_state_snapshot_json={},
                sources=self._sources(), statements=self._statements(),
                reproducibility_status="fully_reproducible",
            )

    def test_v2_attribution_persists_exactly_and_is_exposed_without_email(self):
        attribution = {
            "type": "person",
            "actors": [{
                "user_id": self.user.id,
                "display_name": "GUTS User",
                "email": "guts@example.test",
            }],
        }
        sources = self._sources() + [{
            "source_id": "opportunity_note:1",
            "source_class": "organizational_knowledge",
            "source_type": "note",
            "authority": "attributed_claim",
            "citation_label": "Internal note",
            "author_display_name": "GUTS User",
            "author_user_id": self.user.id,
            "author_address": "guts@example.test",
        }]
        statements = self._statements() + [{
            "statement_key": "organizational_knowledge-1",
            "placement_type": "section",
            "section_type": "organizational_knowledge",
            "section_title": "Organizational Knowledge",
            "position": 1,
            "text": "GUTS User recommended early outreach.",
            "importance": "normal",
            "confidence": "attributed",
            "attribution_json": attribution,
            "source_ids": ["opportunity_note:1"],
        }]
        with (
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_PROMPT_VERSION", "guts-v9"),
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_OUTPUT_SCHEMA_VERSION", "guts-output-v2"),
        ):
            generation = self._pending()
        mark_generation_running(self.db, generation)
        saved = save_generation_success(
            self.db, generation, output_json={"briefing": {}},
            current_state_snapshot_json={"title": "GUTS-1"}, sources=sources,
            statements=statements, reproducibility_status="fully_reproducible",
        )
        row = next(item for item in saved.statements if item.confidence == "attributed")
        self.assertEqual(row.attribution_json, attribution)
        attribution["actors"][0]["display_name"] = "Mutated Caller Value"
        self.assertEqual(row.attribution_json["actors"][0]["display_name"], "GUTS User")

    def test_v2_invalid_attribution_rolls_back_atomic_success(self):
        with (
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_PROMPT_VERSION", "guts-v9"),
            patch("bidlens.services.opportunity_knowledge_brief.repository.config.GUTS_OUTPUT_SCHEMA_VERSION", "guts-output-v2"),
        ):
            generation = self._pending()
        mark_generation_running(self.db, generation)
        invalid = self._statements()
        invalid[0]["confidence"] = "attributed"
        with self.assertRaises(KnowledgeBriefValidationError):
            save_generation_success(
                self.db, generation, output_json={}, current_state_snapshot_json={},
                sources=self._sources(), statements=invalid,
                reproducibility_status="fully_reproducible",
            )
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatement).count(), 0)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefSource).count(), 0)

    def test_latest_success_is_scoped_and_later_failure_does_not_replace_it(self):
        first = self._succeed(
            self._pending(), completed_at=datetime(2026, 7, 31, 10, tzinfo=timezone.utc),
        )
        failed = mark_generation_running(self.db, self._pending())
        mark_generation_failed(self.db, failed, failure_category="model_timeout")
        latest = get_latest_successful_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        )
        self.assertEqual(latest.id, first.id)
        self.assertIsNone(get_latest_successful_generation(
            self.db, organization_id=self.other_org.id, opportunity_id=self.opportunity.id,
        ))
        second = self._succeed(
            self._pending(), completed_at=datetime(2026, 7, 31, 11, tzinfo=timezone.utc),
        )
        self.assertEqual(get_latest_successful_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        ).id, second.id)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefGeneration).count(), 3)

    def test_stale_expiration_only_expires_old_active_attempt(self):
        now = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
        generation = self._pending(requested_at=now - timedelta(minutes=20))
        self.assertTrue(expire_stale_generation(self.db, generation, max_age_seconds=900, now=now))
        self.assertEqual(generation.status, GenerationStatus.FAILED)
        self.assertEqual(generation.failure_category, FailureCategory.STALE_ATTEMPT)
        self.assertFalse(expire_stale_generation(self.db, generation, max_age_seconds=900, now=now))

    def test_save_validation_rejects_duplicates_unknown_citations_and_raw_body(self):
        generation = mark_generation_running(self.db, self._pending())
        cases = [
            (self._sources() * 2, self._statements()),
            (self._sources(), self._statements() * 2),
            (self._sources(), [{**self._statements()[0], "source_ids": ["missing:1"]}]),
            ([{**self._sources()[0], "body": "raw source text"}], self._statements()),
            (self._sources(), [{**self._statements()[0], "source_ids": []}]),
        ]
        for sources, statements in cases:
            with self.subTest(sources=sources, statements=statements):
                with self.assertRaises(KnowledgeBriefValidationError):
                    save_generation_success(
                        self.db, generation, output_json={}, current_state_snapshot_json={},
                        sources=sources, statements=statements,
                        reproducibility_status="fully_reproducible",
                    )
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefSource).count(), 0)

    def test_repository_rejects_unknown_contract_values(self):
        generation = mark_generation_running(self.db, self._pending())
        with self.assertRaises(KnowledgeBriefValidationError):
            mark_generation_failed(self.db, generation, failure_category="made_up_failure")
        invalid_source = [{**self._sources()[0], "authority": "untrusted_guess"}]
        with self.assertRaises(KnowledgeBriefValidationError):
            save_generation_success(
                self.db, generation, output_json={}, current_state_snapshot_json={},
                sources=invalid_source, statements=self._statements(),
                reproducibility_status="fully_reproducible",
            )
        invalid_statement = [{**self._statements()[0], "confidence": "probably"}]
        with self.assertRaises(KnowledgeBriefValidationError):
            save_generation_success(
                self.db, generation, output_json={}, current_state_snapshot_json={},
                sources=self._sources(), statements=invalid_statement,
                reproducibility_status="fully_reproducible",
            )
        with self.assertRaises(KnowledgeBriefValidationError):
            save_generation_success(
                self.db, generation, output_json={}, current_state_snapshot_json={},
                sources=self._sources(), statements=self._statements(),
                reproducibility_status="mostly_reproducible",
            )

    def test_simulated_commit_failure_rolls_back_children_and_success_transition(self):
        generation = mark_generation_running(self.db, self._pending())
        with patch.object(self.db, "commit", side_effect=RuntimeError("simulated persistence failure")):
            with self.assertRaises(RuntimeError):
                save_generation_success(
                    self.db, generation, output_json={"ok": True}, current_state_snapshot_json={"title": "GUTS-1"},
                    sources=self._sources(), statements=self._statements(),
                    reproducibility_status="fully_reproducible",
                )
        self.db.expire_all()
        loaded = self.db.get(OpportunityKnowledgeBriefGeneration, generation.id)
        self.assertEqual(loaded.status, GenerationStatus.RUNNING)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefSource).count(), 0)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatement).count(), 0)

    def test_database_uniqueness_constraints_reject_duplicate_children(self):
        generation = self._succeed(self._pending())
        source = generation.sources[0]
        statement = generation.statements[0]
        self.db.add(OpportunityKnowledgeBriefSource(
            generation_id=generation.id, source_id=source.source_id, source_class="current_state",
            source_type="opportunity", authority="authoritative_current", citation_label="Duplicate",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()
        self.db.add(OpportunityKnowledgeBriefStatement(
            generation_id=generation.id, statement_key=statement.statement_key, placement_type="summary",
            position=1, text="Duplicate", importance="normal", confidence="supported",
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()
        self.db.add(OpportunityKnowledgeBriefStatementSource(
            statement_id=statement.id, brief_source_id=source.id, position=1,
        ))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_models_have_no_raw_source_or_provider_body_columns(self):
        generation_columns = set(OpportunityKnowledgeBriefGeneration.__table__.columns.keys())
        source_columns = set(OpportunityKnowledgeBriefSource.__table__.columns.keys())
        forbidden = {"raw_prompt", "raw_manifest_text", "raw_source_text", "raw_provider_response", "body"}
        self.assertFalse(generation_columns & forbidden)
        self.assertFalse(source_columns & forbidden)


if __name__ == "__main__":
    unittest.main()
