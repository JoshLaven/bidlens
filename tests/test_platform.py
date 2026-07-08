import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.auth import is_platform_admin_email, platform_admin_emails
from bidlens.database import Base
from bidlens.models import Event, Organization, OrganizationMembership, User, WorkspaceInvitation
from bidlens.services.platform import (
    PROFESSIONAL_INCLUDED_USERS,
    ProvisionWorkspaceInput,
    accept_workspace_invitation,
    provision_workspace,
)


class PlatformProvisioningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_provision_workspace_creates_customer_architecture(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="ABC Services Inc.",
                owner_name="Avery Owner",
                owner_email="avery@example.com",
            ),
            platform_user_id=None,
            base_url="https://bidlens.test",
        )

        org = self.db.query(Organization).filter(Organization.id == result.organization.id).one()
        owner = self.db.query(User).filter(User.email == "avery@example.com").one()
        membership = (
            self.db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == owner.id,
            )
            .one()
        )

        self.assertEqual(org.name, "ABC Services Inc.")
        self.assertFalse(org.is_live)
        self.assertEqual(org.plan, "professional")
        self.assertEqual(result.workspace.organization_id, org.id)
        self.assertEqual(result.workspace.plan.code, "professional")
        self.assertEqual(result.plan.included_user_count, PROFESSIONAL_INCLUDED_USERS)
        self.assertEqual(owner.organization_id, org.id)
        self.assertEqual(membership.role, "admin")
        self.assertEqual(result.invitation.status, "pending")
        self.assertIn("/invite/", result.invitation_url)
        self.assertIn("Welcome to BidLens", result.email_placeholder)

    def test_accept_workspace_invitation_marks_invite_and_preserves_setup_state(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Setup Customer",
                owner_name="Olivia Owner",
                owner_email="olivia@example.com",
                operational_contact_is_owner=False,
                operational_contact_name="Ops Contact",
                operational_contact_email="ops@example.com",
                billing_contact_name="Billing Contact",
                billing_contact_email="billing@example.com",
            ),
            base_url="https://bidlens.test",
        )

        accepted = accept_workspace_invitation(self.db, token=result.invitation.token)
        invitation = self.db.get(WorkspaceInvitation, result.invitation.id)

        self.assertIsNotNone(accepted)
        self.assertEqual(invitation.status, "accepted")
        self.assertIsNotNone(invitation.accepted_at)
        self.assertFalse(result.organization.is_live)
        self.assertEqual(result.workspace.operational_contact_email, "ops@example.com")
        self.assertEqual(result.workspace.billing_contact_email, "billing@example.com")

        accepted_user = self.db.query(User).filter(User.email == "olivia@example.com").one()
        acceptance_event = (
            self.db.query(Event)
            .filter(
                Event.org_id == result.organization.id,
                Event.user_id == accepted_user.id,
                Event.event_type == "workspace_invitation_accepted",
            )
            .one()
        )
        self.assertTrue(acceptance_event.payload["development_acceptance"])

    def test_accept_workspace_invitation_creates_missing_workspace_owner(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Missing Owner Customer",
                owner_name="Morgan Missing",
                owner_email="morgan@example.com",
            ),
            base_url="https://bidlens.test",
        )
        self.db.delete(result.membership)
        self.db.delete(result.owner)
        self.db.commit()

        accepted = accept_workspace_invitation(self.db, token=result.invitation.token)
        recreated_owner = self.db.query(User).filter(User.email == "morgan@example.com").one()
        membership = (
            self.db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == result.organization.id,
                OrganizationMembership.user_id == recreated_owner.id,
            )
            .one()
        )

        self.assertIsNotNone(accepted)
        self.assertEqual(recreated_owner.organization_id, result.organization.id)
        self.assertEqual(recreated_owner.name, "Morgan Missing")
        self.assertEqual(membership.role, "admin")

    def test_platform_owner_email_env_identifies_platform_admin(self):
        with patch.dict(
            "os.environ",
            {
                "PLATFORM_OWNER_EMAIL": "Josh@BidLens.test",
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ):
            self.assertIn("josh@bidlens.test", platform_admin_emails())
            self.assertTrue(is_platform_admin_email("josh@bidlens.test"))
            self.assertFalse(is_platform_admin_email("workspace-owner@example.com"))


if __name__ == "__main__":
    unittest.main()
