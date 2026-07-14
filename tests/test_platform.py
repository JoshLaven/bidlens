import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.auth import is_platform_admin_email, platform_admin_emails
from bidlens.database import Base
from fastapi import HTTPException

from bidlens.models import Event, Organization, OrganizationMembership, User, Workspace, WorkspaceInvitation
from bidlens.routes import auth as auth_routes
from bidlens.routes import platform as platform_routes
from bidlens.services.platform import (
    PROFESSIONAL_INCLUDED_USERS,
    ProvisionWorkspaceInput,
    accept_workspace_invitation,
    post_authentication_destination_url,
    post_invitation_acceptance_url,
    provision_workspace,
)
from bidlens.tenancy import duplicate_domain_diagnostics, organization_for_email_domain


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

    def test_workspace_owner_acceptance_routes_to_setup_until_go_live(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Setup Route Customer",
                owner_name="Riley Owner",
                owner_email="riley@example.com",
            ),
            base_url="https://bidlens.test",
        )

        accepted = accept_workspace_invitation(self.db, token=result.invitation.token)

        self.assertEqual(
            post_invitation_acceptance_url(self.db, accepted),
            f"/organization-setup?org_id={result.organization.id}",
        )

    def test_workspace_owner_acceptance_routes_to_home_after_go_live(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Live Route Customer",
                owner_name="Logan Owner",
                owner_email="logan@example.com",
            ),
            base_url="https://bidlens.test",
        )
        result.organization.is_live = True
        self.db.commit()

        accepted = accept_workspace_invitation(self.db, token=result.invitation.token)

        self.assertEqual(
            post_invitation_acceptance_url(self.db, accepted),
            f"/home?org_id={result.organization.id}",
        )

    def test_member_acceptance_routes_to_home_even_before_go_live(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Member Route Customer",
                owner_name="Avery Owner",
                owner_email="avery.member-route@example.com",
            ),
            base_url="https://bidlens.test",
        )
        invitation = WorkspaceInvitation(
            organization_id=result.organization.id,
            workspace_id=result.workspace.id,
            email="member@example.com",
            name="Member Person",
            role="member",
            token="member-token",
            status="pending",
        )
        self.db.add(invitation)
        self.db.commit()

        accepted = accept_workspace_invitation(self.db, token=invitation.token)

        self.assertEqual(
            post_invitation_acceptance_url(self.db, accepted),
            f"/home?org_id={result.organization.id}",
        )

    def test_accept_invitation_route_uses_canonical_post_acceptance_destination(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Route Handler Customer",
                owner_name="Jordan Owner",
                owner_email="jordan@example.com",
            ),
            base_url="https://bidlens.test",
        )

        response = asyncio.run(platform_routes.accept_invitation(
            result.invitation.token,
            SimpleNamespace(),
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/organization-setup?org_id={result.organization.id}",
        )

    def test_pre_live_owner_login_routes_to_organization_setup(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Owner Login Customer",
                owner_name="Pat Owner",
                owner_email="pat@owner-login.test",
            ),
            base_url="https://bidlens.test",
        )

        response = asyncio.run(auth_routes.login(
            SimpleNamespace(),
            email=result.owner.email,
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/organization-setup?org_id={result.organization.id}",
        )

    def test_pre_live_workspace_admin_login_routes_to_organization_setup(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Admin Login Customer",
                owner_name="Casey Owner",
                owner_email="casey@admin-login.test",
            ),
            base_url="https://bidlens.test",
        )
        admin = User(email="teammate@admin-login.test", organization_id=result.organization.id)
        self.db.add(admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=result.organization.id,
            user_id=admin.id,
            role="admin",
        ))
        self.db.commit()

        response = asyncio.run(auth_routes.login(
            SimpleNamespace(),
            email=admin.email,
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/organization-setup?org_id={result.organization.id}",
        )

    def test_live_workspace_admin_login_routes_to_home(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Live Login Customer",
                owner_name="Robin Owner",
                owner_email="robin@live-login.test",
            ),
            base_url="https://bidlens.test",
        )
        result.organization.is_live = True
        self.db.commit()

        response = asyncio.run(auth_routes.login(
            SimpleNamespace(),
            email=result.owner.email,
            db=self.db,
        ))

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/home?org_id={result.organization.id}",
        )

    def test_invitation_acceptance_and_post_auth_use_same_destination(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Canonical Route Customer",
                owner_name="Taylor Owner",
                owner_email="taylor@canonical-route.test",
            ),
            base_url="https://bidlens.test",
        )
        accepted = accept_workspace_invitation(self.db, token=result.invitation.token)

        self.assertEqual(
            post_invitation_acceptance_url(self.db, accepted),
            post_authentication_destination_url(
                self.db,
                result.owner,
                organization_id=result.organization.id,
            ),
        )

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

    def test_email_domain_resolution_prefers_workspace_backed_organization(self):
        orphan = Organization(
            name="Orphaned Office",
            slug="orphaned-office",
            email_domain="theoffice.com",
        )
        provisioned = Organization(
            name="The Office",
            slug="the-office",
            email_domain="theoffice.com",
        )
        self.db.add_all([orphan, provisioned])
        self.db.flush()
        self.db.add(Workspace(
            organization_id=provisioned.id,
            name="The Office Workspace",
            slug="the-office",
        ))
        self.db.commit()

        resolved = organization_for_email_domain(self.db, "jim@theoffice.com")

        self.assertEqual(resolved.id, provisioned.id)

    def test_email_domain_resolution_fails_for_orphaned_duplicate_domain(self):
        self.db.add_all([
            Organization(name="First Office", slug="first-office", email_domain="dupe.test"),
            Organization(name="Second Office", slug="second-office", email_domain="dupe.test"),
        ])
        self.db.commit()

        with self.assertRaises(HTTPException) as raised:
            organization_for_email_domain(self.db, "user@dupe.test")

        self.assertEqual(raised.exception.status_code, 409)

    def test_duplicate_domain_diagnostic_marks_orphaned_organizations(self):
        orphan = Organization(
            name="Orphaned Office",
            slug="orphaned-office",
            email_domain="theoffice.com",
        )
        provisioned = Organization(
            name="The Office",
            slug="the-office",
            email_domain="theoffice.com",
        )
        self.db.add_all([orphan, provisioned])
        self.db.flush()
        self.db.add(Workspace(
            organization_id=provisioned.id,
            name="The Office Workspace",
            slug="the-office",
        ))
        self.db.commit()

        diagnostics = duplicate_domain_diagnostics(self.db)

        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["email_domain"], "theoffice.com")
        self.assertEqual(diagnostics[0]["orphaned_organization_ids"], [orphan.id])
        self.assertEqual(diagnostics[0]["workspace_organization_ids"], [provisioned.id])

    def test_normal_login_does_not_self_provision_customer_organization(self):
        response = asyncio.run(auth_routes.login(
            SimpleNamespace(),
            email="newperson@notprovisioned.test",
            db=self.db,
        ))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db.query(Organization).count(), 0)
        self.assertEqual(self.db.query(User).count(), 0)


if __name__ == "__main__":
    unittest.main()
