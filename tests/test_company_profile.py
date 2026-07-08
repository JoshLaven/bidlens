import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import CompanyProfile, Organization, OrganizationMembership, User
from bidlens.routes.company_profile import (
    active_company_profile,
    archive_duplicate_active_profiles,
    company_profile_save,
    upsert_company_profile,
)


class CompanyProfileTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Profile Org", slug="profile-org")
        self.db.add(self.org)
        self.db.flush()
        self.user = User(email="owner@profile.test", organization_id=self.org.id)
        self.db.add(self.user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.user.id,
            role="admin",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_upsert_updates_existing_active_profile(self):
        profile, created, archived_count = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://example.com",
            uei="ORIGINALUEI12",
            cage_code="1ABC2",
            duns="123456789",
        )
        self.db.commit()

        updated, updated_created, updated_archived_count = upsert_company_profile(
            self.db,
            org_id=self.org.id,
            website_url="https://updated.example.com",
            uei="UPDATEDUEI12",
            cage_code="9XYZ8",
            duns="987654321",
        )
        self.db.commit()

        active_profiles = (
            self.db.query(CompanyProfile)
            .filter(CompanyProfile.org_id == self.org.id, CompanyProfile.archived_at.is_(None))
            .all()
        )

        self.assertTrue(created)
        self.assertFalse(updated_created)
        self.assertEqual(archived_count, 0)
        self.assertEqual(updated_archived_count, 0)
        self.assertEqual(profile.id, updated.id)
        self.assertEqual(len(active_profiles), 1)
        self.assertEqual(active_profiles[0].company_name, self.org.name)
        self.assertEqual(active_profiles[0].uei, "UPDATEDUEI12")
        self.assertEqual(active_profiles[0].cage_code, "9XYZ8")
        self.assertEqual(active_profiles[0].duns, "987654321")
        self.assertEqual(active_profiles[0].profile_json["profile_type"], "organization_identity")

    def test_duplicate_active_profiles_are_archived(self):
        older = CompanyProfile(
            org_id=self.org.id,
            company_name="Older",
            profile_json={"profile_type": "organization_identity"},
        )
        newer = CompanyProfile(
            org_id=self.org.id,
            company_name="Newer",
            profile_json={"profile_type": "organization_identity"},
        )
        self.db.add_all([older, newer])
        self.db.commit()

        archived_count = archive_duplicate_active_profiles(
            self.db,
            org_id=self.org.id,
            keep_profile_id=newer.id,
        )
        self.db.commit()

        active = active_company_profile(self.db, self.org.id)

        self.assertEqual(archived_count, 1)
        self.assertEqual(active.id, newer.id)
        self.assertIsNotNone(self.db.get(CompanyProfile, older.id).archived_at)

    def test_save_redirects_back_to_organization_setup(self):
        request = SimpleNamespace(query_params={})
        setattr(self.user, "current_organization_id", self.org.id)

        with patch("bidlens.routes.company_profile.get_current_user", return_value=self.user):
            response = asyncio.run(company_profile_save(
                request,
                website_url="https://profile.example.com",
                uei="PROFILEUEI12",
                cage_code="1PROF",
                duns="123123123",
                db=self.db,
            ))

        active = active_company_profile(self.db, self.org.id)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/home?org_id={self.org.id}&profile_saved=1")
        self.assertIsNotNone(active)
        self.assertEqual(active.website_url, "https://profile.example.com")


if __name__ == "__main__":
    unittest.main()
