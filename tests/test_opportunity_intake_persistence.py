import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityIntakeDraft,
    OpportunitySourceMaterial,
    Organization,
    OrganizationMembership,
    User,
    Workspace,
)
from bidlens.services.opportunity_intake import (
    DraftAccessError,
    LocalSourceMaterialStorage,
    SourceMaterialStorageError,
    SourceMaterialValidationError,
    configured_source_material_storage,
    create_draft,
    expire_abandoned_drafts,
    get_draft,
    preserve_materials_for_opportunity,
    sanitize_original_filename,
    store_source_material,
    update_draft,
)


class OpportunityIntakePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalSourceMaterialStorage(Path(self.tmp.name))

        self.org = Organization(name="Intake Org", slug="intake-org")
        self.other_org = Organization(name="Other Org", slug="other-intake-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Intake", slug="intake")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="other-intake")
        self.user = User(email="member@example.com", organization_id=self.org.id)
        self.other_user = User(email="other@example.com", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.user, self.other_user])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.other_user.id, role="member"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _draft(self, **overrides):
        values = {
            "organization_id": self.org.id,
            "workspace_id": self.workspace.id,
            "user_id": self.user.id,
            "intake_method": "document",
            "candidate": {"title": "Candidate", "response_deadline": "2026-08-31"},
        }
        values.update(overrides)
        return create_draft(self.db, **values)

    def _opportunity(self, record_id="INTAKE-1"):
        opportunity = Opportunity(
            organization_id=self.org.id,
            source="user_intake",
            source_record_id=record_id,
            solicitation_number=record_id,
            title="Published Opportunity",
            agency="Client",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 28),
            response_deadline=date(2026, 8, 31),
            qualification_status="qualified",
        )
        self.db.add(opportunity)
        self.db.flush()
        return opportunity

    def test_draft_defaults_and_internal_reference_are_persisted(self):
        draft = self._draft()
        self.db.commit()
        loaded = self.db.get(OpportunityIntakeDraft, draft.id)
        self.assertEqual(loaded.intake_method, "document")
        self.assertEqual(loaded.status, "DRAFT")
        self.assertTrue(loaded.add_to_shortlist)
        self.assertEqual(loaded.candidate_fields_json["title"], "Candidate")
        self.assertEqual(loaded.internal_reference, f"BL-{loaded.created_at.year}-{loaded.id:06d}")

    def test_internal_references_and_publish_idempotency_are_unique(self):
        first = self._draft(publish_idempotency_key="same-key")
        second = self._draft(publish_idempotency_key="other-key")
        self.assertNotEqual(first.internal_reference, second.internal_reference)
        self.db.commit()
        with self.assertRaises(IntegrityError):
            self._draft(publish_idempotency_key="same-key")
        self.db.rollback()

    def test_workspace_and_creator_scope_deny_cross_access(self):
        draft = self._draft()
        with self.assertRaises(DraftAccessError):
            get_draft(
                self.db,
                draft_id=draft.id,
                organization_id=self.other_org.id,
                workspace_id=self.other_workspace.id,
                user_id=self.other_user.id,
            )
        with self.assertRaises(DraftAccessError):
            create_draft(
                self.db,
                organization_id=self.org.id,
                workspace_id=self.other_workspace.id,
                user_id=self.user.id,
                intake_method="manual",
            )

    def test_update_draft_persists_candidate_status_and_checkbox(self):
        draft = self._draft()
        update_draft(
            self.db,
            draft_id=draft.id,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            candidate={"title": "Reviewed", "client": "Client", "response_deadline": "2026-09-01"},
            status="ready",
            add_to_shortlist=False,
        )
        self.assertEqual(draft.status, "READY")
        self.assertEqual(draft.candidate_fields_json["title"], "Reviewed")
        self.assertFalse(draft.add_to_shortlist)

    def test_source_material_and_parent_child_relationships(self):
        draft = self._draft()
        parent = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="email", original_filename="message.eml",
            content=b"email body", mime_type="message/rfc822", internet_message_id="<one@example.com>",
        )
        child = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="attachment", original_filename="brief.pdf",
            content=b"pdf bytes", mime_type="application/pdf", parent_material_id=parent.id,
        )
        self.db.commit()
        self.assertEqual(child.parent_material_id, parent.id)
        self.assertEqual(parent.child_materials[0].id, child.id)
        self.assertEqual(child.intake_draft_id, draft.id)
        self.assertTrue(self.storage.exists(child.storage_key))

    def test_storage_keys_and_filenames_are_safe_and_hash_is_stable(self):
        draft = self._draft()
        content = b"stable content"
        first = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="rfp", original_filename="../../secret.pdf",
            content=content, mime_type="application/pdf",
        )
        second = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="rfp", original_filename="secret.pdf",
            content=content, mime_type="application/pdf",
        )
        self.assertEqual(first.original_filename, "secret.pdf")
        self.assertNotIn("secret.pdf", first.storage_key)
        self.assertTrue(first.storage_key.startswith(f"org-{self.org.id}/workspace-{self.workspace.id}/draft-{draft.id}/"))
        self.assertNotEqual(first.storage_key, second.storage_key)
        self.assertEqual(first.sha256_digest, second.sha256_digest)
        self.assertEqual(self.storage.get(first.storage_key), content)
        for unsafe in ("../escape", "/absolute", "org-1/../../escape"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(SourceMaterialStorageError):
                    self.storage.put(unsafe, b"bad")

    def test_empty_and_oversized_materials_are_rejected_without_storage(self):
        draft = self._draft()
        common = {
            "draft_id": draft.id,
            "organization_id": self.org.id,
            "workspace_id": self.workspace.id,
            "user_id": self.user.id,
            "material_type": "rfp",
            "original_filename": "file.pdf",
        }
        with self.assertRaises(SourceMaterialValidationError):
            store_source_material(self.db, self.storage, content=b"", **common)
        with self.assertRaises(SourceMaterialValidationError):
            store_source_material(self.db, self.storage, content=b"1234", max_file_bytes=3, **common)
        self.assertEqual(self.db.query(OpportunitySourceMaterial).count(), 0)
        self.assertEqual(list(Path(self.tmp.name).rglob("*")), [])

    def test_configured_default_file_size_limit_is_enforced(self):
        draft = self._draft()
        with patch("bidlens.services.opportunity_intake.drafts.config.SOURCE_MATERIAL_MAX_BYTES", 3):
            with self.assertRaises(SourceMaterialValidationError):
                store_source_material(
                    self.db, self.storage,
                    draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
                    user_id=self.user.id, material_type="rfp", original_filename="file.pdf",
                    content=b"1234",
                )

    def test_expiration_deletes_unassociated_material_and_storage(self):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        draft = self._draft(expires_at=now - timedelta(minutes=1), now=now - timedelta(days=8))
        material = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="rfp", original_filename="old.pdf", content=b"old",
        )
        key = material.storage_key
        result = expire_abandoned_drafts(self.db, self.storage, now=now)
        self.assertEqual(result.drafts_expired, 1)
        self.assertEqual(result.materials_deleted, 1)
        self.assertFalse(self.storage.exists(key))
        self.assertIsNone(self.db.get(OpportunityIntakeDraft, draft.id))
        self.assertIsNone(self.db.get(OpportunitySourceMaterial, material.id))

    def test_opportunity_associated_material_survives_abandoned_draft_cleanup(self):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        draft = self._draft(expires_at=now - timedelta(minutes=1), now=now - timedelta(days=8))
        material = store_source_material(
            self.db, self.storage,
            draft_id=draft.id, organization_id=self.org.id, workspace_id=self.workspace.id,
            user_id=self.user.id, material_type="rfp", original_filename="retained.pdf", content=b"retained",
        )
        opportunity = self._opportunity()
        self.assertEqual(preserve_materials_for_opportunity(self.db, draft=draft, opportunity=opportunity), 1)
        result = expire_abandoned_drafts(self.db, self.storage, now=now)
        self.assertEqual(result.materials_preserved, 1)
        retained = self.db.get(OpportunitySourceMaterial, material.id)
        self.assertEqual(retained.opportunity_id, opportunity.id)
        self.assertIsNone(retained.intake_draft_id)
        self.assertTrue(self.storage.exists(retained.storage_key))

    def test_published_draft_is_not_expired(self):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        draft = self._draft(expires_at=now - timedelta(minutes=1), now=now - timedelta(days=8))
        draft.status = "PUBLISHED"
        result = expire_abandoned_drafts(self.db, self.storage, now=now)
        self.assertEqual(result.drafts_expired, 0)
        self.assertIsNotNone(self.db.get(OpportunityIntakeDraft, draft.id))

    def test_local_storage_is_rejected_for_hosted_configuration(self):
        with patch("bidlens.services.opportunity_intake.storage.config.deployment_validation_enabled", return_value=True):
            with patch("bidlens.services.opportunity_intake.storage.config.SOURCE_MATERIAL_STORAGE_BACKEND", "local"):
                with self.assertRaises(SourceMaterialStorageError):
                    configured_source_material_storage()

    def test_filename_sanitizer_handles_windows_and_empty_names(self):
        self.assertEqual(sanitize_original_filename(r"C:\Users\name\rfp.docx"), "rfp.docx")
        self.assertEqual(sanitize_original_filename("../"), "upload")


if __name__ == "__main__":
    unittest.main()
