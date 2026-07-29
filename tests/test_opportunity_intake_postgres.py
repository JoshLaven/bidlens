import os
import threading
import unittest
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.config import database_target_summary
from bidlens.models import Opportunity, OpportunitySourceMaterial, Organization, OrganizationMembership, User, Vote, Workspace
from bidlens.services.opportunity_intake import OpportunityDuplicateError, OpportunityPublisher, create_draft


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_DATABASE_URL", "").strip()


def _sanitized_database_failure(action, exc):
    return RuntimeError(
        f"PostgreSQL test {action} failed "
        f"target=({database_target_summary(POSTGRES_TEST_URL)}) error_type={type(exc).__name__}"
    )


@unittest.skipUnless(POSTGRES_TEST_URL, "TEST_POSTGRES_DATABASE_URL is not configured")
class PostgreSQLOpportunityPublisherConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.admin_engine = create_engine(POSTGRES_TEST_URL, pool_pre_ping=True)
            if cls.admin_engine.dialect.name != "postgresql":
                raise unittest.SkipTest("TEST_POSTGRES_DATABASE_URL must use PostgreSQL")
            cls.schema = f"bidlens_intake_test_{uuid.uuid4().hex}"
            with cls.admin_engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
            cls.engine = create_engine(
                POSTGRES_TEST_URL,
                pool_pre_ping=True,
                connect_args={"options": f"-csearch_path={cls.schema}"},
            )
            Base.metadata.create_all(cls.engine)
            cls.Session = sessionmaker(bind=cls.engine, expire_on_commit=False)
        except unittest.SkipTest:
            raise
        except Exception as exc:
            raise _sanitized_database_failure("setup", exc) from None

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "engine"):
            cls.engine.dispose()
        if hasattr(cls, "admin_engine") and hasattr(cls, "schema"):
            try:
                with cls.admin_engine.begin() as connection:
                    connection.execute(text(f'DROP SCHEMA IF EXISTS "{cls.schema}" CASCADE'))
            except Exception as exc:
                raise _sanitized_database_failure("schema cleanup", exc) from None
            finally:
                cls.admin_engine.dispose()

    def setUp(self):
        suffix = uuid.uuid4().hex[:12]
        db = self.Session()
        self.org = Organization(name=f"Concurrency {suffix}", slug=f"concurrency-{suffix}")
        db.add(self.org)
        db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Concurrency", slug=f"workspace-{suffix}")
        self.user = User(email=f"publisher-{suffix}@example.test", organization_id=self.org.id)
        db.add_all([self.workspace, self.user])
        db.flush()
        db.add(OrganizationMembership(organization_id=self.org.id, user_id=self.user.id, role="member"))
        db.commit()
        db.close()

    def _draft(self, *, material=False):
        db = self.Session()
        draft = create_draft(
            db,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.user.id,
            intake_method="document" if material else "manual",
        )
        if material:
            db.add(OpportunitySourceMaterial(
                organization_id=self.org.id,
                workspace_id=self.workspace.id,
                intake_draft_id=draft.id,
                material_type="rfp_document",
                original_filename="concurrency.pdf",
                mime_type="application/pdf",
                byte_size=10,
                sha256_digest=uuid.uuid4().hex * 2,
                storage_key=f"org-{self.org.id}/workspace-{self.workspace.id}/draft-{draft.id}/{uuid.uuid4().hex}",
                parse_status="COMPLETE",
            ))
        db.commit()
        draft_id = draft.id
        db.close()
        return draft_id

    def _publish_thread(self, barrier, draft_id, key, solicitation, results, errors):
        db = self.Session()
        try:
            user = db.get(User, self.user.id)
            barrier.wait(timeout=10)
            result = OpportunityPublisher.publish_reviewed_draft(
                db,
                draft_id=draft_id,
                publishing_user=user,
                reviewed_candidate={
                    "title": f"Concurrent {solicitation}",
                    "client": "Concurrency Client",
                    "response_deadline": "2026-12-31",
                    "solicitation_number": solicitation,
                    "opportunity_type": "RFP",
                },
                add_to_shortlist=True,
                idempotency_key=key,
            )
            results.append(result.opportunity_id)
        except OpportunityDuplicateError as exc:
            errors.append(exc)
        except Exception as exc:
            errors.append(_sanitized_database_failure("publish", exc))
        finally:
            db.close()

    def test_same_draft_concurrent_replay_creates_one_opportunity_vote_and_association(self):
        draft_id = self._draft(material=True)
        key = f"same-{uuid.uuid4().hex}"
        solicitation = f"PG-SAME-{uuid.uuid4().hex}"
        barrier = threading.Barrier(2)
        results, errors = [], []
        threads = [
            threading.Thread(target=self._publish_thread, args=(barrier, draft_id, key, solicitation, results, errors))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertFalse(errors)
        self.assertEqual(len(set(results)), 1)
        db = self.Session()
        opportunity_id = results[0]
        self.assertEqual(db.query(Opportunity).filter_by(id=opportunity_id).count(), 1)
        self.assertEqual(db.query(Vote).filter_by(opp_id=opportunity_id, vote="PURSUE").count(), 1)
        self.assertEqual(db.query(OpportunitySourceMaterial).filter_by(intake_draft_id=draft_id, opportunity_id=opportunity_id).count(), 1)
        db.close()

    def test_two_draft_exact_duplicate_race_creates_one_opportunity(self):
        draft_ids = [self._draft(), self._draft()]
        solicitation = f"PG-RACE-{uuid.uuid4().hex}"
        barrier = threading.Barrier(2)
        results, errors = [], []
        threads = [
            threading.Thread(
                target=self._publish_thread,
                args=(barrier, draft_id, f"key-{draft_id}-{uuid.uuid4().hex}", solicitation, results, errors),
            )
            for draft_id in draft_ids
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], OpportunityDuplicateError)
        db = self.Session()
        self.assertEqual(db.query(Opportunity).filter_by(organization_id=self.org.id, solicitation_number=solicitation).count(), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
