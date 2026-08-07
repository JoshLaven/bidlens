import datetime as dt
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity, OpportunityKnowledgeBriefGeneration, OpportunityKnowledgeBriefSource,
    OpportunityKnowledgeBriefStatement, OpportunityKnowledgeBriefStatementSource,
    Organization, OrganizationMembership, User, Vote, Workspace,
)
from bidlens.services.opportunity_knowledge_brief import (
    GUTSModelCallResult, GUTSModelError, GUTSServiceError, GenerationStatus,
    OpportunityKnowledgeBriefCompiler, OpportunityKnowledgeBriefService,
    create_pending_generation, get_latest_successful_generation,
)
from bidlens.services.opportunity_knowledge_brief.contracts import (
    AttributionActor, EvidenceAuthor, EvidenceCollectionResult, EvidenceSource,
    ModelBriefingOutput, ModelOutputStatement, OfficialEvidenceCollectionResult,
    StatementAttribution, UnavailableSource,
)


UTC = dt.timezone.utc


def empty_collection():
    return EvidenceCollectionResult(
        evidence=(), available_count=0, selected_count=0, excluded_count=0,
        truncated=False, omitted_reason_counts={}, latest_source_at=None,
        total_selected_characters=0,
    )


def empty_official(*, unavailable=(), evidence=(), contains_unretained=False):
    return OfficialEvidenceCollectionResult(
        evidence=tuple(evidence), available_count=len(evidence), selected_count=len(evidence),
        excluded_count=0, truncated=False, omitted_reason_counts={},
        latest_source_at=max((item.occurred_at for item in evidence if item.occurred_at), default=None),
        total_selected_characters=sum(len(item.text) for item in evidence),
        unavailable_sources=tuple(unavailable), contains_unretained_external=contains_unretained,
    )


def communication_collection(opportunity_id, organization_id, workspace_id):
    text = "Cassie has done similar work. I will forward this."
    source = EvidenceSource(
        source_id="communication:1", source_class="organizational_knowledge",
        source_type="email", authority="attributed_claim",
        citation_label="Communication from Josh", text=text,
        author=EvidenceAuthor(
            user_id=1, display_name="Josh", address="josh@example.test",
        ),
        content_hash="c" * 64, selected_character_count=len(text), original_character_count=len(text),
        retained_by_bidlens=True,
        provenance={
            "organization_id": organization_id, "workspace_id": workspace_id,
            "opportunity_id": opportunity_id,
        },
    )
    return EvidenceCollectionResult(
        evidence=(source,), available_count=1, selected_count=1, excluded_count=0,
        truncated=False, omitted_reason_counts={}, latest_source_at=None,
        total_selected_characters=len(text),
    )


class Collector:
    def __init__(self, result): self.result = result
    def collect(self, **kwargs): return self.result


class FailingCollector:
    def collect(self, **kwargs):
        raise RuntimeError("PRIVATE SOURCE CONTENT")


class FakeModelClient:
    def __init__(self, output=None, error=None):
        self.output = output; self.error = error; self.calls = 0
    def generate(self, manifest):
        self.calls += 1
        if self.error: raise self.error
        return GUTSModelCallResult(self.output, "openai", "guts-test", 100, 30, 130, 12.4)
    def retry_with_validation_feedback(self, manifest, feedback):
        self.calls += 1
        if self.error: raise self.error
        return GUTSModelCallResult(self.output, "openai", "guts-test", 100, 30, 130, 12.4)


def model_output(opportunity_id):
    return ModelBriefingOutput(
        headline=ModelOutputStatement(
            statement_key="headline", text="Evaluation Services is active.", importance="high",
            confidence="supported", source_ids=(f"current_state:opportunity:{opportunity_id}:source_stage",),
        ),
        summary_statements=(ModelOutputStatement(
            statement_key="summary-1", text="The response deadline is September 1, 2026.",
            importance="normal", confidence="supported",
            source_ids=(f"current_state:opportunity:{opportunity_id}:response_deadline",),
        ),),
        sections=(),
    )


class GUTSSession6Tests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.org = Organization(name="Compiler Org", slug="compiler-org")
        self.db.add(self.org); self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Compiler", slug="compiler")
        self.member = User(email="compiler@example.test", organization_id=self.org.id)
        self.admin = User(email="compiler-admin@example.test", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.member, self.admin]); self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.member.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.admin.id, role="admin"),
        ])
        self.opportunity = Opportunity(
            organization_id=self.org.id, source="test", source_record_id="COMPILER-1",
            solicitation_number="RFP-100", title="Evaluation Services", agency="Example Agency",
            description="The agency seeks evaluation support services for its national program.",
            opportunity_type="RFP", source_stage="active", posted_date=dt.date(2026, 7, 1),
            response_deadline=dt.date(2026, 9, 1), qualification_status="qualified",
        )
        self.db.add(self.opportunity); self.db.flush()
        self.db.add(Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.member.id, vote="PURSUE"))
        self.db.commit()
        for user, role in ((self.member, "member"), (self.admin, "admin")):
            user.current_organization_id = self.org.id; user.current_role = role

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    def compiler(self, *, model=None, official=None, notes=None, communications=None, history=None):
        return OpportunityKnowledgeBriefCompiler(
            self.db, model_client=model or FakeModelClient(model_output(self.opportunity.id)),
            official_collector=Collector(official or empty_official()),
            note_collector=Collector(notes or empty_collection()),
            communication_collector=Collector(communications or empty_collection()),
            history_collector=Collector(history or empty_collection()),
        )

    def service(self, **kwargs):
        return OpportunityKnowledgeBriefService(self.db, compiler=self.compiler(**kwargs))

    def generate(self, service=None):
        with patch("bidlens.services.opportunity_knowledge_brief.service.config.GUTS_ENABLED", True):
            return (service or self.service()).generate(
                opportunity_id=self.opportunity.id, requesting_user=self.member,
                active_organization_id=self.org.id,
            )

    def test_current_state_only_success_persists_complete_graph_and_metadata(self):
        generation = self.generate()
        self.assertEqual(generation.status, GenerationStatus.SUCCEEDED)
        self.assertEqual(generation.provider, "openai")
        self.assertEqual((generation.input_tokens, generation.output_tokens, generation.total_tokens), (100, 30, 130))
        self.assertEqual(generation.validation_retry_count, 0)
        self.assertEqual(len(generation.sources), 19)
        self.assertEqual(len(generation.statements), 2)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatementSource).count(), 2)
        self.assertIsNotNone(generation.manifest_hash)
        self.assertIsNotNone(generation.source_snapshot_started_at)
        self.assertIsNotNone(generation.source_snapshot_completed_at)
        self.assertIsNotNone(generation.current_state_snapshot_json)
        self.assertIn("briefing", generation.output_json)
        self.assertEqual(generation.source_summary_json["communications"]["used"], 0)
        self.assertEqual(generation.warning_metadata_json, [])
        self.assertIsNotNone(generation.total_ms)
        self.assertIsNotNone(generation.persistence_ms)
        latest = get_latest_successful_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        )
        self.assertEqual(latest.id, generation.id)
        self.assertEqual(latest.statements[0].source_links[0].position, 0)

    def test_degraded_and_partially_reproducible_success_builds_warnings_and_counts(self):
        source = EvidenceSource(
            source_id="external_document:sam:1", source_class="official_evidence",
            source_type="solicitation_document", authority="official_source",
            citation_label="Official document", text="Official evaluation requirements.",
            content_hash="a" * 64, selected_character_count=33, original_character_count=33,
            retained_by_bidlens=False, provider="sam",
            provenance={"organization_id": self.org.id, "workspace_id": self.workspace.id, "opportunity_id": self.opportunity.id},
        )
        unavailable = UnavailableSource(
            source_id="external_document:missing:1", source_type="solicitation_document",
            failure_category="source_retrieval_failed", safe_message="Official source unavailable.",
            retryable=True, provenance={"organization_id": self.org.id},
        )
        official = empty_official(unavailable=(unavailable,), evidence=(source,), contains_unretained=True)
        generation = self.generate(self.service(official=official))
        self.assertEqual(generation.reproducibility_status, "partially_reproducible")
        self.assertEqual(generation.degraded_source_count, 1)
        self.assertEqual({item["type"] for item in generation.warning_metadata_json}, {
            "missing_source", "partial_generation", "truncated_input", "not_fully_reproducible",
        })
        self.assertEqual(generation.statistics_json["official_sources"], 1)
        self.assertEqual(generation.source_summary_json["official_documents"], {"available": 1, "used": 1, "failed": 1})
        persisted = self.db.query(OpportunityKnowledgeBriefSource).filter_by(source_id=source.source_id).one()
        self.assertFalse(persisted.retained_by_bidlens)
        self.assertFalse(any(item.source_id == unavailable.source_id for item in generation.sources))

    def test_selected_unused_communication_adds_non_blocking_development_warning(self):
        communications = communication_collection(
            self.opportunity.id, self.org.id, self.workspace.id,
        )
        generation = self.generate(self.service(communications=communications))
        self.assertEqual(generation.status, GenerationStatus.SUCCEEDED)
        warning = next(
            item for item in generation.warning_metadata_json
            if item["type"] == "communication_evidence_unused"
        )
        self.assertEqual(warning["visibility"], "development")
        self.assertEqual(warning["metadata"], {
            "selected_communications": 1,
            "communication_derived_statements": 0,
        })
        self.assertEqual(generation.statistics_json["selected_communications"], 1)
        self.assertEqual(generation.statistics_json["communication_derived_statements"], 0)

    def test_communication_citation_suppresses_unused_warning(self):
        communications = communication_collection(
            self.opportunity.id, self.org.id, self.workspace.id,
        )
        output = model_output(self.opportunity.id).model_copy(update={
            "summary_statements": (
                ModelOutputStatement(
                    statement_key="communication-memory",
                    text="Josh identified Cassie as having done similar work.",
                    importance="normal", confidence="attributed",
                    source_ids=("communication:1",),
                    attribution=StatementAttribution(
                        type="person", actors=(AttributionActor(
                            user_id=1, display_name="Josh", email="josh@example.test",
                        ),),
                    ),
                ),
            ),
        })
        generation = self.generate(self.service(
            communications=communications, model=FakeModelClient(output),
        ))
        self.assertEqual(generation.status, GenerationStatus.SUCCEEDED)
        self.assertFalse(any(
            item["type"] == "communication_evidence_unused"
            for item in generation.warning_metadata_json
        ))
        self.assertEqual(generation.statistics_json["selected_communications"], 1)
        self.assertEqual(generation.statistics_json["communication_derived_statements"], 1)

    def test_feature_and_shortlist_policy_reject_before_attempt(self):
        with patch("bidlens.services.opportunity_knowledge_brief.service.config.GUTS_ENABLED", False):
            with self.assertRaises(GUTSServiceError) as disabled:
                self.service().generate(
                    opportunity_id=self.opportunity.id, requesting_user=self.member,
                    active_organization_id=self.org.id,
                )
        self.assertEqual(disabled.exception.safe_category, "access_denied")
        self.db.query(Vote).delete(); self.db.commit()
        for user in (self.member, self.admin):
            with patch("bidlens.services.opportunity_knowledge_brief.service.config.GUTS_ENABLED", True):
                with self.assertRaises(GUTSServiceError) as rejected:
                    self.service().generate(
                        opportunity_id=self.opportunity.id, requesting_user=user,
                        active_organization_id=self.org.id,
                    )
            self.assertEqual(rejected.exception.safe_category, "shortlist_required")
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefGeneration).count(), 0)

    def test_active_attempt_rejected_and_stale_attempt_replaced(self):
        active = create_pending_generation(
            self.db, organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opportunity.id, generated_by_user_id=self.member.id,
        )
        with self.assertRaises(GUTSServiceError) as rejected:
            self.generate()
        self.assertEqual(rejected.exception.safe_category, "generation_already_in_progress")
        active.requested_at = dt.datetime.now(UTC) - dt.timedelta(seconds=1000); self.db.commit()
        replacement = self.generate()
        self.db.refresh(active)
        self.assertEqual(active.failure_category, "stale_attempt")
        self.assertEqual(replacement.status, "succeeded")

    def test_model_failure_marks_attempt_failed_and_preserves_prior_success(self):
        prior = self.generate()
        error = GUTSModelError("model_timeout", "The GUTS model request timed out.", retryable=True)
        with self.assertRaises(GUTSServiceError) as failed:
            self.generate(self.service(model=FakeModelClient(error=error)))
        self.assertEqual(failed.exception.safe_category, "model_timeout")
        attempt = self.db.get(OpportunityKnowledgeBriefGeneration, failed.exception.generation_id)
        self.assertEqual(attempt.status, "failed")
        self.assertEqual(attempt.failure_stage, "model_call")
        self.assertIsNone(attempt.output_json)
        self.assertEqual(get_latest_successful_generation(
            self.db, organization_id=self.org.id, opportunity_id=self.opportunity.id,
        ).id, prior.id)

    def test_collection_and_success_persistence_failures_are_safely_finalized(self):
        collection_compiler = self.compiler()
        collection_compiler.note_collector = FailingCollector()
        with self.assertLogs("bidlens.services.opportunity_knowledge_brief.compiler", level="WARNING") as captured:
            with self.assertRaises(GUTSServiceError) as collection_failure:
                self.generate(OpportunityKnowledgeBriefService(self.db, compiler=collection_compiler))
        self.assertEqual(collection_failure.exception.safe_category, "source_collection_failed")
        self.assertNotIn("PRIVATE SOURCE CONTENT", "\n".join(captured.output))
        first_attempt = self.db.get(OpportunityKnowledgeBriefGeneration, collection_failure.exception.generation_id)
        self.assertEqual(first_attempt.status, "failed")

        with patch(
            "bidlens.services.opportunity_knowledge_brief.compiler.save_generation_success",
            side_effect=RuntimeError("PRIVATE MODEL OUTPUT"),
        ):
            with self.assertRaises(GUTSServiceError) as persistence_failure:
                self.generate()
        self.assertEqual(persistence_failure.exception.safe_category, "persistence_failed")
        second_attempt = self.db.get(OpportunityKnowledgeBriefGeneration, persistence_failure.exception.generation_id)
        self.assertEqual(second_attempt.status, "failed")
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefSource).count(), 0)
        self.assertEqual(self.db.query(OpportunityKnowledgeBriefStatement).count(), 0)

    def test_insufficient_evidence_fails_without_model_call(self):
        self.opportunity.title = "TBD"; self.opportunity.agency = ""
        self.opportunity.description = None
        self.opportunity.source_stage = None; self.opportunity.solicitation_number = None
        self.db.commit()
        model = FakeModelClient(model_output(self.opportunity.id))
        with self.assertRaises(GUTSServiceError) as failed:
            self.generate(self.service(model=model))
        self.assertEqual(failed.exception.safe_category, "insufficient_evidence")
        self.assertEqual(model.calls, 0)
        attempt = self.db.get(OpportunityKnowledgeBriefGeneration, failed.exception.generation_id)
        self.assertEqual(attempt.status, "failed")

    def test_unchanged_state_has_stable_hash_and_refresh_creates_new_attempt(self):
        first = self.generate()
        second = self.generate()
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        self.opportunity.description += " Additional factual context."
        self.db.commit()
        third = self.generate()
        self.assertNotEqual(second.manifest_hash, third.manifest_hash)


if __name__ == "__main__":
    unittest.main()
