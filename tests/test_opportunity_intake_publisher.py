import unittest
from datetime import date, datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    IngestionRun,
    IngestionRunDetail,
    Opportunity,
    OpportunityHistoryEvent,
    OpportunityIntakeDraft,
    OpportunityPursuitLaneMatch,
    OpportunitySourceMaterial,
    OrgProfile,
    Organization,
    OrganizationMembership,
    PursuitLane,
    User,
    Vote,
    Workspace,
)
from bidlens.services.feed_queries import build_feed_query
from bidlens.services.opportunity_intake import (
    OpportunityDuplicateError,
    OpportunityPublicationAccessError,
    OpportunityPublicationConflict,
    OpportunityPublicationValidationError,
    OpportunityPublisher,
    create_draft,
)


class OpportunityPublisherTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.org = Organization(name="Publisher Org", slug="publisher-org")
        self.other_org = Organization(name="Other Publisher Org", slug="other-publisher-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Publisher", slug="publisher")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id, name="Other Publisher", slug="other-publisher"
        )
        self.user = User(email="publisher@example.com", organization_id=self.org.id)
        self.colleague = User(email="colleague@example.com", organization_id=self.org.id)
        self.outsider = User(email="outsider@example.com", organization_id=self.other_org.id)
        self.db.add_all([
            self.workspace, self.other_workspace, self.user, self.colleague, self.outsider
        ])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.colleague.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.outsider.id, role="member"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _draft(self, *, org=None, workspace=None, user=None, candidate=None, shortlist=True):
        org = org or self.org
        workspace = workspace or self.workspace
        user = user or self.user
        draft = create_draft(
            self.db,
            organization_id=org.id,
            workspace_id=workspace.id,
            user_id=user.id,
            intake_method="manual",
            candidate=candidate or {
                "title": "Stored title",
                "client": "Stored client",
                "response_deadline": "2026-09-15",
            },
            add_to_shortlist=shortlist,
        )
        self.db.commit()
        return draft

    def _review(self, **overrides):
        values = {
            "title": "Reviewed title",
            "client": "Reviewed client",
            "response_deadline": "2026-09-30",
            "solicitation_number": "RFP-2026-42",
            "description": "Reviewed description",
        }
        values.update(overrides)
        return values

    def test_reviewed_canonical_type_is_persisted_independently_of_stage(self):
        draft = self._draft()
        result = self._publish(
            draft,
            review=self._review(opportunity_type="Forecast", canonical_type="Cooperative Agreement"),
        )
        opportunity = self.db.get(Opportunity, result.opportunity_id)
        self.assertEqual(opportunity.opportunity_type, "Forecast")
        self.assertEqual(opportunity.canonical_type, "Cooperative Agreement")

    def _publish(self, draft, *, shortlist=False, key=None, user=None, review=None):
        return OpportunityPublisher.publish_reviewed_draft(
            self.db,
            draft_id=draft.id,
            publishing_user=user or self.user,
            reviewed_candidate=review or self._review(),
            add_to_shortlist=shortlist,
            idempotency_key=key or f"publish-{draft.id}",
            saved_on=date(2026, 7, 28),
            now=datetime(2026, 7, 28, 12, 0),
        )

    def _material(self, draft, *, digest, provider_id=None, internet_id=None, opportunity=None):
        material = OpportunitySourceMaterial(
            organization_id=draft.organization_id,
            workspace_id=draft.workspace_id,
            intake_draft_id=draft.id,
            opportunity_id=opportunity.id if opportunity else None,
            material_type="email",
            original_filename=f"{digest}.eml",
            byte_size=10,
            sha256_digest=digest,
            storage_key=f"org-{draft.organization_id}/draft-{draft.id}/{digest}",
            provider_message_id=provider_id,
            internet_message_id=internet_id,
        )
        self.db.add(material)
        self.db.commit()
        return material

    def _existing_opportunity(self, org=None, **overrides):
        org = org or self.org
        values = {
            "organization_id": org.id,
            "source": "sam",
            "source_record_id": f"existing-{org.id}-{self.db.query(Opportunity).count()}",
            "solicitation_number": "EXISTING-1",
            "title": "Existing title",
            "agency": "Existing client",
            "opportunity_type": "RFP",
            "posted_date": date(2026, 7, 1),
            "response_deadline": date(2026, 9, 30),
            "qualification_status": "qualified",
            "decision_state": "INBOX",
        }
        values.update(overrides)
        opportunity = Opportunity(**values)
        self.db.add(opportunity)
        self.db.commit()
        return opportunity

    def test_publishes_reviewed_values_directly_to_feed_and_not_triage(self):
        self.db.add(OrgProfile(org_id=self.org.id, triage_enabled=True))
        self.db.commit()
        draft = self._draft(shortlist=True)
        result = self._publish(draft, shortlist=False)
        opportunity = self.db.get(Opportunity, result.opportunity_id)
        self.assertEqual(opportunity.title, "Reviewed title")
        self.assertEqual(opportunity.agency, "Reviewed client")
        self.assertEqual(opportunity.qualification_status, "qualified")
        self.assertEqual(opportunity.decision_state, "INBOX")
        self.assertEqual(opportunity.source, "user_intake")
        self.assertEqual(opportunity.source_record_id, draft.internal_reference)
        self.assertEqual(opportunity.solicitation_number, "RFP-2026-42")
        self.assertIsNotNone(build_feed_query(
            self.db, organization_id=self.org.id, user_id=self.user.id, include_watched=False
        ).filter(Opportunity.id == opportunity.id).one_or_none())
        self.assertEqual(self.db.query(Opportunity).filter(
            Opportunity.id == opportunity.id,
            Opportunity.qualification_status == "unreviewed",
        ).count(), 0)

    def test_explicit_false_overrides_default_and_other_member_sees_feed(self):
        draft = self._draft(shortlist=True)
        result = self._publish(draft, shortlist=False)
        self.assertFalse(result.added_to_shortlist)
        self.assertEqual(self.db.query(Vote).count(), 0)
        self.assertIsNotNone(build_feed_query(
            self.db, organization_id=self.org.id, user_id=self.colleague.id, include_watched=False
        ).filter(Opportunity.id == result.opportunity_id).one_or_none())

    def test_checked_adds_one_vote_and_personal_feed_excludes_it(self):
        draft = self._draft()
        first = self._publish(draft, shortlist=True)
        replay = self._publish(draft, shortlist=True)
        self.assertEqual(first.opportunity_id, replay.opportunity_id)
        self.assertEqual(self.db.query(Opportunity).count(), 1)
        self.assertEqual(self.db.query(Vote).filter(Vote.vote == "PURSUE").count(), 1)
        self.assertIsNotNone(self.db.query(Vote).filter(Vote.vote == "PURSUE").one().shortlisted_at)
        self.assertIsNone(build_feed_query(
            self.db, organization_id=self.org.id, user_id=self.user.id, include_watched=False
        ).filter(Opportunity.id == first.opportunity_id).one_or_none())
        self.assertIsNotNone(build_feed_query(
            self.db, organization_id=self.org.id, user_id=self.colleague.id, include_watched=False
        ).filter(Opportunity.id == first.opportunity_id).one_or_none())

    def test_blank_solicitation_uses_persisted_reference(self):
        draft = self._draft()
        result = self._publish(draft, review=self._review(solicitation_number=""))
        self.assertEqual(result.solicitation_number, draft.internal_reference)
        self.assertEqual(result.source_record_id, draft.internal_reference)

    def test_validation_failure_creates_nothing(self):
        draft = self._draft()
        with self.assertRaises(OpportunityPublicationValidationError):
            self._publish(draft, review={"title": "", "client": "", "response_deadline": "bad"})
        self.assertEqual(self.db.query(Opportunity).count(), 0)
        self.assertEqual(self.db.get(OpportunityIntakeDraft, draft.id).status, "DRAFT")

    def test_same_org_exact_solicitation_blocks_but_other_org_is_ignored(self):
        self._existing_opportunity(org=self.other_org, solicitation_number="RFP 2026 42")
        draft = self._draft()
        self._publish(draft)
        second = self._draft()
        with self.assertRaises(OpportunityDuplicateError) as raised:
            self._publish(second, key="second-key")
        self.assertEqual(raised.exception.duplicates.exact_matches[0].reason, "solicitation_number")
        self.assertEqual(self.db.query(Opportunity).filter(
            Opportunity.organization_id == self.org.id
        ).count(), 1)

    def test_material_hash_and_message_identifiers_block_duplicates(self):
        for field in ("hash", "provider", "internet"):
            with self.subTest(field=field):
                existing_draft = self._draft()
                existing = self._existing_opportunity(
                    source_record_id=f"existing-{field}", solicitation_number=f"existing-{field}"
                )
                kwargs = {
                    "digest": f"digest-{field}-existing",
                    "provider_id": "provider-42" if field == "provider" else None,
                    "internet_id": "<internet-42@example.com>" if field == "internet" else None,
                }
                if field == "hash":
                    kwargs["digest"] = "same-hash"
                self._material(existing_draft, opportunity=existing, **kwargs)
                draft = self._draft()
                incoming = dict(kwargs)
                if field != "hash":
                    incoming["digest"] = f"digest-{field}-incoming"
                self._material(draft, **incoming)
                with self.assertRaises(OpportunityDuplicateError) as raised:
                    self._publish(
                        draft,
                        key=f"duplicate-{field}",
                        review=self._review(solicitation_number=f"new-{field}"),
                    )
                reasons = {match.reason for match in raised.exception.duplicates.exact_matches}
                self.assertIn({
                    "hash": "source_material_sha256",
                    "provider": "provider_message_id",
                    "internet": "internet_message_id",
                }[field], reasons)

    def test_material_duplicate_is_detected_after_draft_link_is_detached(self):
        existing_draft = self._draft()
        existing = self._existing_opportunity(
            source_record_id="detached-existing", solicitation_number="detached-existing"
        )
        material = self._material(
            existing_draft,
            opportunity=existing,
            digest="detached-source-hash",
        )
        material.intake_draft_id = None
        self.db.commit()
        draft = self._draft()
        self._material(draft, digest="detached-source-hash")
        with self.assertRaises(OpportunityDuplicateError):
            self._publish(
                draft,
                key="detached-duplicate",
                review=self._review(solicitation_number="detached-new"),
            )

    def test_probable_match_is_reported_without_blocking(self):
        self._existing_opportunity(
            title="Reviewed title", agency="Reviewed client", response_deadline=date(2026, 9, 30)
        )
        draft = self._draft()
        result = self._publish(draft)
        self.assertEqual(result.metadata["probable_duplicates"][0]["reason"], "title_client_deadline")

    def test_source_material_history_lane_and_audit_commit_together(self):
        lane = PursuitLane(
            organization_id=self.org.id, name="Reviewed", keywords=["Reviewed title"], is_active=True
        )
        self.db.add(lane)
        self.db.commit()
        draft = self._draft()
        material = self._material(draft, digest="unique-material")
        result = self._publish(draft, shortlist=True)
        loaded_draft = self.db.get(OpportunityIntakeDraft, draft.id)
        self.assertEqual(loaded_draft.status, "PUBLISHED")
        self.assertEqual(loaded_draft.published_opportunity_id, result.opportunity_id)
        self.assertEqual(self.db.get(OpportunitySourceMaterial, material.id).opportunity_id, result.opportunity_id)
        self.assertEqual(self.db.query(OpportunityHistoryEvent).count(), 1)
        self.assertEqual(self.db.query(OpportunityPursuitLaneMatch).count(), 1)
        self.assertEqual(self.db.query(Vote).count(), 1)
        self.assertEqual(self.db.query(IngestionRun).count(), 1)
        self.assertEqual(self.db.query(IngestionRunDetail).count(), 1)

    def test_failure_during_material_association_rolls_back_everything(self):
        draft = self._draft()
        with patch(
            "bidlens.services.opportunity_intake.publisher.preserve_materials_for_opportunity",
            side_effect=RuntimeError("association failed"),
        ):
            with self.assertRaises(RuntimeError):
                self._publish(draft)
        self.assertEqual(self.db.query(Opportunity).count(), 0)
        loaded = self.db.get(OpportunityIntakeDraft, draft.id)
        self.assertEqual(loaded.status, "DRAFT")
        self.assertIsNone(loaded.published_opportunity_id)

    def test_nonmember_cross_workspace_and_noncreator_are_denied(self):
        draft = self._draft()
        for user in (self.outsider, self.colleague):
            with self.subTest(user=user.email):
                with self.assertRaises(OpportunityPublicationAccessError):
                    self._publish(draft, user=user, key=f"denied-{user.id}")
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_already_published_draft_rejects_different_key(self):
        draft = self._draft()
        self._publish(draft, key="first-key")
        with self.assertRaises(OpportunityPublicationConflict):
            self._publish(draft, key="different-key")
        self.assertEqual(self.db.query(Opportunity).count(), 1)


if __name__ == "__main__":
    unittest.main()
