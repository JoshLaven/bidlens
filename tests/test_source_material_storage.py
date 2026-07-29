import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bidlens import auth, config, main
from bidlens.database import Base, get_db
from bidlens.models import Opportunity, OpportunityIntakeDraft, OpportunitySourceMaterial, Organization, OrganizationMembership, User, Workspace
from bidlens.services.opportunity_intake import (
    LocalSourceMaterialStorage,
    S3SourceMaterialStorage,
    SourceMaterialStorageError,
    configured_source_material_storage,
    create_draft,
    expire_abandoned_drafts,
    preserve_materials_for_opportunity,
    reconcile_source_materials,
    store_source_material,
)


class _Body:
    def __init__(self, content):
        self.content = content
        self.closed = False

    def read(self):
        return self.content

    def iter_chunks(self, chunk_size):
        for offset in range(0, len(self.content), chunk_size):
            yield self.content[offset : offset + chunk_size]

    def close(self):
        self.closed = True


class _MissingObject(Exception):
    response = {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.fail_operation = None

    def _fail(self, operation):
        if self.fail_operation == operation:
            raise RuntimeError(f"{operation} failed")

    def put_object(self, *, Bucket, Key, Body, ContentLength):
        self._fail("put")
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        self._fail("get")
        try:
            return {"Body": _Body(self.objects[(Bucket, Key)])}
        except KeyError as exc:
            raise _MissingObject() from exc

    def delete_object(self, *, Bucket, Key):
        self._fail("delete")
        self.objects.pop((Bucket, Key), None)

    def head_object(self, *, Bucket, Key):
        self._fail("head")
        try:
            content = self.objects[(Bucket, Key)]
        except KeyError as exc:
            raise _MissingObject() from exc
        return {"ContentLength": len(content), "ETag": '"test-etag"'}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self._fail("list")
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket and key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}


class S3StorageBackendTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3Client()
        self.storage = S3SourceMaterialStorage(
            bucket="private-bucket",
            endpoint_url="https://storage.example.test",
            region="us-east-1",
            access_key_id="unused-with-client",
            secret_access_key="unused-with-client",
            path_prefix="bidlens/source-materials",
            client=self.client,
        )
        self.key = "org-1/workspace-2/draft-3/0123456789abcdef"

    def test_put_get_stream_metadata_exists_list_and_delete(self):
        content = b"binary\x00content" * 10000
        self.storage.put(self.key, content)
        self.assertTrue(self.storage.exists(self.key))
        self.assertEqual(self.storage.get(self.key), content)
        self.assertEqual(b"".join(self.storage.iter_bytes(self.key, chunk_size=1024)), content)
        metadata = self.storage.metadata(self.key)
        self.assertEqual(metadata.content_length, len(content))
        self.assertEqual(metadata.etag, "test-etag")
        self.assertEqual(list(self.storage.list_keys("org-1/workspace-2/")), [self.key])
        self.storage.delete(self.key)
        self.assertFalse(self.storage.exists(self.key))

    def test_unsafe_keys_and_provider_failures_are_mapped(self):
        for key in ("../escape", "/absolute", "org-1/../../escape"):
            with self.subTest(key=key), self.assertRaises(SourceMaterialStorageError):
                self.storage.put(key, b"bad")
        self.client.fail_operation = "put"
        with self.assertRaisesRegex(SourceMaterialStorageError, "upload failed"):
            self.storage.put(self.key, b"content")
        self.assertFalse(self.client.objects)

    def test_provider_credentials_are_not_chained_into_storage_errors(self):
        credential = "storage-secret-that-must-not-leak"

        class CredentialLeakingClient(FakeS3Client):
            def put_object(self, **kwargs):
                raise RuntimeError(f"request failed url=https://access:{credential}@storage.example.test")

        storage = S3SourceMaterialStorage(
            bucket="private-bucket",
            endpoint_url="https://storage.example.test",
            region="us-east-1",
            access_key_id="unused-with-client",
            secret_access_key="unused-with-client",
            client=CredentialLeakingClient(),
        )

        with self.assertRaises(SourceMaterialStorageError) as raised:
            storage.put(self.key, b"content")

        self.assertNotIn(credential, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_denied_storage_operations_are_sanitized_and_remain_visible(self):
        self.storage.put(self.key, b"content")
        operations = (
            ("get", lambda: self.storage.get(self.key), "download failed"),
            ("delete", lambda: self.storage.delete(self.key), "delete failed"),
            ("head", lambda: self.storage.metadata(self.key), "metadata lookup failed"),
            ("list", lambda: list(self.storage.list_keys("org-1/")), "object listing failed"),
        )

        for failure, action, message in operations:
            with self.subTest(operation=failure):
                self.client.fail_operation = failure
                with self.assertRaisesRegex(SourceMaterialStorageError, message) as raised:
                    action()
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)

    def test_client_initialization_error_does_not_chain_credentials(self):
        credential = "initialization-secret-that-must-not-leak"
        with patch("boto3.client", side_effect=RuntimeError(f"endpoint password={credential}")):
            with self.assertRaises(SourceMaterialStorageError) as raised:
                S3SourceMaterialStorage(
                    bucket="private-bucket",
                    endpoint_url="https://storage.example.test",
                    region="us-east-1",
                    access_key_id="access-key",
                    secret_access_key=credential,
                )

        self.assertNotIn(credential, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_configured_s3_rejects_incomplete_config_and_local_remains_available(self):
        with patch("bidlens.services.opportunity_intake.storage.config.SOURCE_MATERIAL_STORAGE_BACKEND", "s3"), patch(
            "bidlens.services.opportunity_intake.storage.config.SOURCE_MATERIAL_S3_BUCKET", ""
        ):
            with self.assertRaises(SourceMaterialStorageError):
                configured_source_material_storage()
        with tempfile.TemporaryDirectory() as directory, patch(
            "bidlens.services.opportunity_intake.storage.config.SOURCE_MATERIAL_STORAGE_BACKEND", "local"
        ), patch(
            "bidlens.services.opportunity_intake.storage.config.SOURCE_MATERIAL_LOCAL_ROOT", Path(directory)
        ), patch(
            "bidlens.services.opportunity_intake.storage.config.deployment_validation_enabled", return_value=False
        ):
            self.assertIsInstance(configured_source_material_storage(), LocalSourceMaterialStorage)


class SourceMaterialHardeningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = LocalSourceMaterialStorage(Path(self.tmp.name))
        self.org = Organization(name="Storage Org", slug="storage-org")
        self.other_org = Organization(name="Other Storage Org", slug="other-storage-org")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Storage", slug="storage")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="other-storage")
        self.creator = User(email="creator@storage.test", organization_id=self.org.id)
        self.member = User(email="member@storage.test", organization_id=self.org.id)
        self.outsider = User(email="outsider@storage.test", organization_id=self.other_org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.creator, self.member, self.outsider])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.creator.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.member.id, role="member"),
            OrganizationMembership(organization_id=self.other_org.id, user_id=self.outsider.id, role="member"),
        ])
        self.db.commit()

        def override_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        main.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()
        self.tmp.cleanup()

    def _login(self, user):
        self.client.cookies.set(config.SESSION_COOKIE_NAME, auth.serializer.dumps({"user_id": user.id}))

    def _draft_material(self, *, expires_at=None):
        draft = create_draft(
            self.db,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.creator.id,
            intake_method="document",
            expires_at=expires_at,
        )
        material = store_source_material(
            self.db,
            self.storage,
            draft_id=draft.id,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.creator.id,
            material_type="rfp_document",
            original_filename='../../résumé "final".pdf',
            content=b"private source bytes",
            mime_type="application/pdf",
        )
        self.db.commit()
        return draft, material

    def _publish_association(self, draft, material):
        opportunity = Opportunity(
            organization_id=self.org.id,
            source="user_intake",
            source_record_id=draft.internal_reference,
            solicitation_number=draft.internal_reference,
            title="Published material",
            agency="Client",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 28),
            response_deadline=date(2026, 8, 30),
            qualification_status="qualified",
            decision_state="INBOX",
        )
        self.db.add(opportunity)
        self.db.flush()
        preserve_materials_for_opportunity(self.db, draft=draft, opportunity=opportunity)
        draft.status = "PUBLISHED"
        draft.published_opportunity_id = opportunity.id
        self.db.commit()
        return opportunity

    def test_draft_creator_downloads_but_other_member_cannot(self):
        _, material = self._draft_material()
        with patch("bidlens.routes.opportunity_intake.configured_source_material_storage", return_value=self.storage):
            self.client.cookies.clear()
            unauthenticated = self.client.get(
                f"/source-materials/{material.id}/download", follow_redirects=False
            )
            self.assertEqual(unauthenticated.status_code, 200)
            self.assertIn("url=/login", unauthenticated.text)
            self._login(self.creator)
            response = self.client.get(f"/source-materials/{material.id}/download")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"private source bytes")
            self.assertEqual(response.headers["content-type"], "application/pdf")
            self.assertIn("attachment;", response.headers["content-disposition"])
            self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            self._login(self.member)
            self.assertEqual(self.client.get(f"/source-materials/{material.id}/download").status_code, 404)

    def test_published_material_is_available_to_member_but_not_cross_tenant(self):
        draft, material = self._draft_material()
        self._publish_association(draft, material)
        with patch("bidlens.routes.opportunity_intake.configured_source_material_storage", return_value=self.storage):
            self._login(self.member)
            self.assertEqual(self.client.get(f"/source-materials/{material.id}/download").status_code, 200)
            self._login(self.outsider)
            self.assertEqual(self.client.get(f"/source-materials/{material.id}/download").status_code, 404)

    def test_cleanup_failure_preserves_metadata_and_retry_is_idempotent(self):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        draft, material = self._draft_material(expires_at=now - timedelta(minutes=1))
        original_delete = self.storage.delete
        with patch.object(self.storage, "delete", side_effect=SourceMaterialStorageError("provider unavailable")):
            result = expire_abandoned_drafts(self.db, self.storage, now=now)
        self.assertEqual(result.storage_failures, 1)
        self.assertIsNotNone(self.db.get(OpportunitySourceMaterial, material.id))
        self.assertIsNotNone(self.db.get(OpportunityIntakeDraft, draft.id))
        result = expire_abandoned_drafts(self.db, self.storage, now=now)
        self.assertEqual(result.drafts_expired, 1)
        self.assertIsNone(self.db.get(OpportunitySourceMaterial, material.id))
        original_delete(material.storage_key)

    def test_metadata_failure_removes_uploaded_object_and_storage_failure_creates_no_metadata(self):
        draft = create_draft(
            self.db,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.creator.id,
            intake_method="document",
        )
        self.db.commit()
        with patch.object(
            self.db,
            "flush",
            side_effect=IntegrityError("insert", {}, RuntimeError("metadata failure")),
        ):
            with self.assertRaises(IntegrityError):
                store_source_material(
                    self.db,
                    self.storage,
                    draft_id=draft.id,
                    organization_id=self.org.id,
                    workspace_id=self.workspace.id,
                    user_id=self.creator.id,
                    material_type="rfp_document",
                    original_filename="failure.pdf",
                    content=b"uploaded first",
                )
        self.db.rollback()
        self.assertEqual([path for path in Path(self.tmp.name).rglob("*") if path.is_file()], [])
        with patch.object(self.storage, "put", side_effect=SourceMaterialStorageError("provider failure")):
            with self.assertRaises(SourceMaterialStorageError):
                store_source_material(
                    self.db,
                    self.storage,
                    draft_id=draft.id,
                    organization_id=self.org.id,
                    workspace_id=self.workspace.id,
                    user_id=self.creator.id,
                    material_type="rfp_document",
                    original_filename="failure.pdf",
                    content=b"not uploaded",
                )
        self.db.rollback()
        self.assertEqual(self.db.query(OpportunitySourceMaterial).count(), 0)
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_reconciliation_reports_missing_unreferenced_and_expired_without_deleting(self):
        now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        _, material = self._draft_material(expires_at=now - timedelta(minutes=1))
        self.storage.delete(material.storage_key)
        orphan_key = f"org-{self.org.id}/workspace-{self.workspace.id}/draft-999/orphan"
        self.storage.put(orphan_key, b"orphan")
        report = reconcile_source_materials(
            self.db,
            self.storage,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            now=now,
        )
        self.assertIn(material.id, report.missing_object_material_ids)
        self.assertIn(orphan_key, report.unreferenced_storage_keys)
        self.assertIn(material.id, report.expired_unpublished_material_ids)
        self.assertTrue(self.storage.exists(orphan_key))


if __name__ == "__main__":
    unittest.main()
