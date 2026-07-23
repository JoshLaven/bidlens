import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from http.cookies import SimpleCookie
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.auth import is_platform_admin_email, platform_admin_emails, serializer
from bidlens.database import Base
from fastapi import HTTPException

from bidlens.models import Event, Organization, OrganizationMembership, User, Workspace, WorkspaceInvitation
from bidlens.routes import auth as auth_routes
from bidlens.routes import platform as platform_routes
from bidlens.services.platform import (
    PROFESSIONAL_INCLUDED_USERS,
    ProvisionWorkspaceInput,
    accept_workspace_invitation,
    create_replacement_workspace_invitation,
    delete_test_organization,
    platform_plan_definitions,
    post_authentication_destination_url,
    post_invitation_acceptance_url,
    provision_workspace,
)
from bidlens.tenancy import (
    current_organization,
    duplicate_domain_diagnostics,
    ensure_email_domain_membership,
    organization_for_email_domain,
    resolve_user_organization,
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

    def test_existing_user_provisioned_as_admin_resolves_to_new_workspace(self):
        original_org = Organization(
            name="Original Acme",
            slug="original-acme",
            email_domain="acme.test",
            is_active=True,
            is_live=True,
        )
        self.db.add(original_org)
        self.db.flush()
        self.db.add(Workspace(
            organization_id=original_org.id,
            name="Original Acme Workspace",
            slug="original-acme-workspace",
        ))
        existing_user = User(email="owner@acme.test", organization_id=original_org.id)
        self.db.add(existing_user)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=original_org.id,
            user_id=existing_user.id,
            role="member",
        ))
        self.db.commit()

        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="New Acme",
                owner_name="Owner Acme",
                owner_email="owner@acme.test",
            ),
            base_url="https://bidlens.test",
        )

        resolved = resolve_user_organization(self.db, result.owner)
        self.assertEqual(resolved.id, result.organization.id)
        membership = (
            self.db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == result.organization.id,
                OrganizationMembership.user_id == result.owner.id,
            )
            .one()
        )
        self.assertEqual(membership.role, "admin")

        response = asyncio.run(auth_routes.login(
            SimpleNamespace(),
            email="  OWNER@ACME.TEST  ",
            db=self.db,
        ))
        self.assertEqual(
            response.headers["location"],
            f"/organization-setup?org_id={result.organization.id}",
        )

    def test_same_domain_workspace_does_not_override_explicit_admin_membership(self):
        first = Organization(name="First Domain", slug="first-domain", email_domain="shared.test", is_active=True, is_live=True)
        second = Organization(name="Second Domain", slug="second-domain", email_domain="shared.test", is_active=True, is_live=False)
        self.db.add_all([first, second])
        self.db.flush()
        self.db.add_all([
            Workspace(organization_id=first.id, name="First Workspace", slug="first-workspace"),
            Workspace(organization_id=second.id, name="Second Workspace", slug="second-workspace"),
        ])
        user = User(email="admin@shared.test", organization_id=second.id)
        self.db.add(user)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=first.id, user_id=user.id, role="member"),
            OrganizationMembership(organization_id=second.id, user_id=user.id, role="admin"),
        ])
        self.db.commit()

        resolved = resolve_user_organization(self.db, user)

        self.assertEqual(resolved.id, second.id)

    def test_domain_auto_membership_does_not_downgrade_existing_admin_role(self):
        org = Organization(name="Domain Org", slug="domain-org", email_domain="domain.test", is_active=True, is_live=True)
        self.db.add(org)
        self.db.flush()
        self.db.add(Workspace(organization_id=org.id, name="Domain Workspace", slug="domain-workspace"))
        user = User(email="Admin@Domain.Test", organization_id=org.id)
        self.db.add(user)
        self.db.flush()
        membership = OrganizationMembership(organization_id=org.id, user_id=user.id, role="admin")
        self.db.add(membership)
        self.db.commit()

        matched = ensure_email_domain_membership(self.db, user)
        self.db.refresh(membership)

        self.assertIsNone(matched)
        self.assertEqual(membership.role, "admin")

    def test_valid_org_id_selection_requires_authorized_membership(self):
        allowed = Organization(name="Allowed", slug="allowed", is_active=True, is_live=True)
        denied = Organization(name="Denied", slug="denied", is_active=True, is_live=True)
        self.db.add_all([allowed, denied])
        self.db.flush()
        self.db.add_all([
            Workspace(organization_id=allowed.id, name="Allowed Workspace", slug="allowed-workspace"),
            Workspace(organization_id=denied.id, name="Denied Workspace", slug="denied-workspace"),
        ])
        user = User(email="switcher@example.test", organization_id=allowed.id)
        self.db.add(user)
        self.db.flush()
        self.db.add(OrganizationMembership(organization_id=allowed.id, user_id=user.id, role="member"))
        self.db.commit()

        request = SimpleNamespace(query_params={"org_id": str(allowed.id)})
        self.assertEqual(current_organization(request, self.db, user).id, allowed.id)
        with self.assertRaises(HTTPException):
            current_organization(SimpleNamespace(query_params={"org_id": str(denied.id)}), self.db, user)

    def test_user_organization_id_is_respected_when_membership_is_valid(self):
        first = Organization(name="First", slug="first", is_active=True, is_live=True)
        second = Organization(name="Second", slug="second", is_active=True, is_live=True)
        self.db.add_all([first, second])
        self.db.flush()
        user = User(email="member@multi.test", organization_id=second.id)
        self.db.add(user)
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=first.id, user_id=user.id, role="member"),
            OrganizationMembership(organization_id=second.id, user_id=user.id, role="member"),
        ])
        self.db.commit()

        resolved = resolve_user_organization(self.db, user)

        self.assertEqual(resolved.id, second.id)

    def test_user_with_no_memberships_can_receive_domain_member_fallback(self):
        org = Organization(name="Fallback", slug="fallback", email_domain="fallback.test", is_active=True, is_live=True)
        self.db.add(org)
        self.db.flush()
        self.db.add(Workspace(organization_id=org.id, name="Fallback Workspace", slug="fallback-workspace"))
        user = User(email="new@fallback.test", organization_id=org.id)
        self.db.add(user)
        self.db.commit()

        resolved = resolve_user_organization(self.db, user)
        membership = (
            self.db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == org.id,
                OrganizationMembership.user_id == user.id,
            )
            .one()
        )

        self.assertEqual(resolved.id, org.id)
        self.assertEqual(membership.role, "member")

    def test_pending_admin_invitation_without_membership_does_not_grant_access(self):
        org = Organization(name="Invite Only", slug="invite-only", is_active=True, is_live=False)
        self.db.add(org)
        self.db.flush()
        workspace = Workspace(organization_id=org.id, name="Invite Only Workspace", slug="invite-only-workspace")
        user = User(email="pending@example.com", organization_id=org.id)
        self.db.add_all([workspace, user])
        self.db.flush()
        self.db.add(WorkspaceInvitation(
            organization_id=org.id,
            workspace_id=workspace.id,
            email="pending@example.com",
            name="Pending Admin",
            role="admin",
            token="pending-admin-token",
            status="pending",
        ))
        self.db.commit()

        self.assertIsNone(resolve_user_organization(self.db, user))

    def test_public_domain_email_does_not_trigger_domain_matching(self):
        org = Organization(name="Gmail Org", slug="gmail-org", email_domain="gmail.com", is_active=True, is_live=True)
        self.db.add(org)
        self.db.flush()
        self.db.add(Workspace(organization_id=org.id, name="Gmail Workspace", slug="gmail-workspace"))
        self.db.commit()

        self.assertIsNone(organization_for_email_domain(self.db, "person@gmail.com"))

    def test_platform_owner_email_env_identifies_platform_admin(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "Josh@BidLens.test",
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ):
            self.assertIn("josh@bidlens.test", platform_admin_emails())
            self.assertTrue(is_platform_admin_email("josh@bidlens.test"))
            self.assertFalse(is_platform_admin_email("workspace-owner@example.com"))

    def test_platform_admin_email_env_identifies_exact_platform_admin(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "",
                "PLATFORM_ADMIN_EMAILS": "admin@bidlens.test, ops@bidlens.test",
            },
            clear=False,
        ):
            self.assertTrue(is_platform_admin_email("admin@bidlens.test"))
            self.assertTrue(is_platform_admin_email("ops@bidlens.test"))
            self.assertFalse(is_platform_admin_email("workspace-admin@bidlens.test"))

    def test_platform_admin_matching_normalizes_case_and_whitespace(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "  Josh@JoshLaven.com  ",
                "PLATFORM_ADMIN_EMAILS": "  Admin@BidLens.test  ",
            },
            clear=False,
        ):
            self.assertTrue(is_platform_admin_email("josh@joshlaven.com"))
            self.assertTrue(is_platform_admin_email(" admin@bidlens.test "))
            self.assertFalse(is_platform_admin_email("xjosh@joshlaven.com"))
            self.assertFalse(is_platform_admin_email("josh@joshlaven.com.example"))

    def test_platform_admin_email_has_no_hardcoded_gmail_default(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "",
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ):
            self.assertEqual(platform_admin_emails(), set())
            self.assertFalse(is_platform_admin_email("joshuatlaven@gmail.com"))

    def test_workspace_admin_role_never_implies_platform_access(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "josh@joshlaven.com",
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ):
            result = provision_workspace(
                self.db,
                payload=ProvisionWorkspaceInput(
                    organization_name="Workspace Admin Customer",
                    owner_name="Workspace Admin",
                    owner_email="joshuatlaven@gmail.com",
                ),
                base_url="https://bidlens.test",
            )

            self.assertEqual(result.membership.role, "admin")
            self.assertFalse(is_platform_admin_email(result.owner.email))

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

    def test_switching_platform_owner_to_workspace_admin_replaces_session_identity(self):
        with patch.dict(
            os.environ,
            {
                "PLATFORM_OWNER_EMAIL": "josh@joshlaven.com",
                "PLATFORM_ADMIN_EMAILS": "",
            },
            clear=False,
        ):
            result = provision_workspace(
                self.db,
                payload=ProvisionWorkspaceInput(
                    organization_name="Session Switch Customer",
                    owner_name="Josh Workspace",
                    owner_email="joshuatlaven@gmail.com",
                ),
                base_url="https://bidlens.test",
            )

            platform_response = asyncio.run(auth_routes.login(
                SimpleNamespace(),
                email="josh@joshlaven.com",
                db=self.db,
            ))
            workspace_response = asyncio.run(auth_routes.login(
                SimpleNamespace(),
                email="joshuatlaven@gmail.com",
                db=self.db,
            ))

            self.assertEqual(platform_response.headers["location"], "/platform")
            self.assertEqual(
                workspace_response.headers["location"],
                f"/organization-setup?org_id={result.organization.id}",
            )

            cookie = SimpleCookie()
            cookie.load(workspace_response.headers["set-cookie"])
            token = cookie["bidlens_session"].value
            session_user_id = serializer.loads(token)["user_id"]

            self.assertEqual(session_user_id, result.owner.id)
            self.assertNotEqual(
                session_user_id,
                self.db.query(User).filter(User.email == "josh@joshlaven.com").one().id,
            )

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

    def _platform_owner(self):
        platform_org = Organization(name="BidLens Platform", slug="bidlens-platform", plan="platform")
        self.db.add(platform_org)
        self.db.flush()
        owner = User(email="joshuatlaven@gmail.com", name="Josh", organization_id=platform_org.id)
        self.db.add(owner)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=platform_org.id,
            user_id=owner.id,
            role="admin",
        ))
        self.db.commit()
        return owner

    def _request(self):
        return SimpleNamespace(
            base_url="https://beta.bidlens.com/",
            query_params={},
        )

    def test_platform_owner_can_open_organization_detail_context(self):
        owner = self._platform_owner()
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Detail Customer",
                owner_name="Dana Owner",
                owner_email="dana@detail.test",
            ),
            base_url="https://beta.bidlens.com",
        )

        context = platform_routes._organization_detail_context(
            self._request(),
            self.db,
            owner,
            result.organization.id,
        )

        self.assertEqual(context["organization"].id, result.organization.id)
        self.assertEqual(context["workspace"].id, result.workspace.id)
        self.assertEqual(context["owner_state"]["status"], "Invitation pending")

    def test_non_platform_user_cannot_open_organization_detail_route(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Forbidden Customer",
                owner_name="Fiona Owner",
                owner_email="fiona@forbidden.test",
            ),
            base_url="https://beta.bidlens.com",
        )

        with patch("bidlens.routes.platform.get_current_user", return_value=result.owner):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(platform_routes.platform_organization_detail(
                    result.organization.id,
                    self._request(),
                    db=self.db,
                ))

        self.assertEqual(raised.exception.status_code, 404)

    def test_platform_organization_list_cards_link_to_detail_route(self):
        owner = self._platform_owner()
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Linked Customer",
                owner_name="Lena Owner",
                owner_email="lena@linked.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        html = platform_routes.templates.env.get_template("platform.html").render(
            request=self._request(),
            user=owner,
            active_page="platform",
            organizations=platform_routes._organization_rows(self.db),
            plans=platform_plan_definitions(),
            selected_plan="professional",
            provisioned=None,
            form={},
            error=None,
        )

        self.assertIn(f'href="/platform/organizations/{result.organization.id}"', html)
        self.assertIn("Linked Customer", html)

    def test_organization_detail_renders_active_and_pending_members(self):
        owner = self._platform_owner()
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Members Customer",
                owner_name="Mina Owner",
                owner_email="mina@members.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        pending = WorkspaceInvitation(
            organization_id=result.organization.id,
            workspace_id=result.workspace.id,
            email="pending@members.test",
            name="Pending Person",
            role="member",
            token="pending-members-token",
            status="pending",
        )
        self.db.add(pending)
        self.db.commit()
        context = platform_routes._organization_detail_context(
            self._request(),
            self.db,
            owner,
            result.organization.id,
        )
        html = platform_routes.templates.env.get_template("platform_organization_detail.html").render(**context)

        self.assertIn("mina@members.test", html)
        self.assertIn("pending@members.test", html)
        self.assertIn("Active Members", html)
        self.assertIn("Pending", html)

    def test_pending_invitation_url_can_be_recovered_with_public_host(self):
        owner = self._platform_owner()
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Invite Customer",
                owner_name="Ira Owner",
                owner_email="ira@invite.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        context = platform_routes._organization_detail_context(
            self._request(),
            self.db,
            owner,
            result.organization.id,
        )
        pending_urls = [row["url"] for row in context["pending_invitations"]]

        self.assertTrue(pending_urls)
        self.assertTrue(pending_urls[0].startswith("https://beta.bidlens.com/invite/"))
        self.assertNotIn("localhost", pending_urls[0])

    def test_replacement_invitation_does_not_create_duplicate_active_membership(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Replacement Customer",
                owner_name="Rae Owner",
                owner_email="rae@replacement.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        accept_workspace_invitation(self.db, token=result.invitation.token)

        with self.assertRaises(ValueError):
            create_replacement_workspace_invitation(self.db, invitation=result.invitation)

        membership_count = (
            self.db.query(OrganizationMembership)
            .filter(OrganizationMembership.organization_id == result.organization.id)
            .count()
        )
        self.assertEqual(membership_count, 1)

    def test_delete_test_organization_requires_confirmation(self):
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Confirm Delete Customer",
                owner_name="Case Owner",
                owner_email="case@delete.test",
            ),
            base_url="https://beta.bidlens.com",
        )

        with self.assertRaises(ValueError):
            delete_test_organization(
                self.db,
                organization_id=result.organization.id,
                confirmation_name="Wrong Name",
            )

        self.assertIsNotNone(self.db.get(Organization, result.organization.id))

    def test_deleting_one_test_organization_does_not_affect_another(self):
        first = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Delete Me Customer",
                owner_name="Dee Owner",
                owner_email="dee@delete-one.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        second = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Keep Me Customer",
                owner_name="Kip Owner",
                owner_email="kip@keep-one.test",
            ),
            base_url="https://beta.bidlens.com",
        )

        delete_test_organization(
            self.db,
            organization_id=first.organization.id,
            confirmation_name="Delete Me Customer",
        )

        self.assertIsNone(self.db.get(Organization, first.organization.id))
        self.assertIsNotNone(self.db.get(Organization, second.organization.id))
        self.assertIsNotNone(self.db.get(Workspace, second.workspace.id))

    def test_default_platform_organization_cannot_be_deleted(self):
        owner = self._platform_owner()

        with self.assertRaises(ValueError):
            delete_test_organization(
                self.db,
                organization_id=owner.organization_id,
                confirmation_name="BidLens Platform",
                platform_admin_user_id=owner.id,
            )

        self.assertIsNotNone(self.db.get(Organization, owner.organization_id))

    def test_platform_owner_identity_is_preserved_when_customer_org_deleted(self):
        owner = self._platform_owner()
        result = provision_workspace(
            self.db,
            payload=ProvisionWorkspaceInput(
                organization_name="Platform Member Customer",
                owner_name="Pat Customer",
                owner_email="pat@platform-member.test",
            ),
            base_url="https://beta.bidlens.com",
        )
        self.db.add(OrganizationMembership(
            organization_id=result.organization.id,
            user_id=owner.id,
            role="admin",
        ))
        owner.organization_id = result.organization.id
        self.db.commit()

        delete_test_organization(
            self.db,
            organization_id=result.organization.id,
            confirmation_name="Platform Member Customer",
            platform_admin_user_id=owner.id,
        )

        preserved_owner = self.db.get(User, owner.id)
        self.assertIsNotNone(preserved_owner)
        self.assertEqual(preserved_owner.email, "joshuatlaven@gmail.com")
        self.assertIsNotNone(self.db.get(Organization, preserved_owner.organization_id))


if __name__ == "__main__":
    unittest.main()
