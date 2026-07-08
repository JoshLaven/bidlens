import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import UploadFile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, OrganizationMembership, User, WorkspaceInvitation
from bidlens.routes.admin import (
    bulk_create_organization_invitations,
    create_organization_invitations,
    delete_organization_invitation,
    list_organization_users,
)
from bidlens.services.platform import accept_workspace_invitation


class WorkspaceMembersTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Members Org", slug="members-org", plan="professional")
        self.db.add(self.org)
        self.db.flush()
        self.admin = User(email="admin@members.test", name="Admin", organization_id=self.org.id)
        self.db.add(self.admin)
        self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id,
            user_id=self.admin.id,
            role="admin",
        ))
        self.db.commit()
        setattr(self.admin, "current_organization_id", self.org.id)
        setattr(self.admin, "current_role", "admin")
        setattr(self.admin, "current_organization_name", self.org.name)
        setattr(self.admin, "current_organization_is_live", False)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self, accept: str = "text/html"):
        return SimpleNamespace(
            query_params={"org_id": str(self.org.id)},
            base_url="https://bidlens.test/",
            headers={"accept": accept},
        )

    def test_quick_invite_creates_pending_invitation_without_user(self):
        request = self._request()
        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            response = create_organization_invitations(
                self.org.id,
                request,
                emails=["new.member@example.com"],
                names=["New Member"],
                roles=["member"],
                db=self.db,
            )

        invitation = self.db.query(WorkspaceInvitation).filter(WorkspaceInvitation.email == "new.member@example.com").one()

        self.assertEqual(response.status_code, 303)
        self.assertEqual(invitation.status, "pending")
        self.assertEqual(invitation.role, "member")
        self.assertIsNone(self.db.query(User).filter(User.email == "new.member@example.com").first())
        self.assertTrue(invitation.token)

    def test_bulk_csv_invite_creates_pending_invitations(self):
        csv_upload = UploadFile(
            filename="members.csv",
            file=io.BytesIO(b"email,name,role\njohn@example.com,John Smith,member\njane@example.com,Jane Smith,admin\n"),
        )
        request = self._request()

        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            response = asyncio.run(bulk_create_organization_invitations(
                self.org.id,
                request,
                csv_file=csv_upload,
                db=self.db,
            ))

        invites = self.db.query(WorkspaceInvitation).order_by(WorkspaceInvitation.email.asc()).all()

        self.assertEqual(response.status_code, 303)
        self.assertEqual([invite.email for invite in invites], ["jane@example.com", "john@example.com"])
        self.assertEqual([invite.role for invite in invites], ["admin", "member"])

    def test_deleted_invitation_cannot_be_accepted(self):
        request = self._request()
        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            create_organization_invitations(
                self.org.id,
                request,
                emails=["delete.me@example.com"],
                names=["Delete Me"],
                roles=["member"],
                db=self.db,
            )
        invitation = self.db.query(WorkspaceInvitation).filter(WorkspaceInvitation.email == "delete.me@example.com").one()

        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            response = delete_organization_invitation(self.org.id, invitation.id, request, db=self.db)

        self.db.refresh(invitation)

        self.assertEqual(response.status_code, 303)
        self.assertEqual(invitation.status, "deleted")
        self.assertIsNone(accept_workspace_invitation(self.db, token=invitation.token))

    def test_members_page_renders_html_with_pending_and_active_sections(self):
        request = self._request()
        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            create_organization_invitations(
                self.org.id,
                request,
                emails=["pending@example.com"],
                names=["Pending Person"],
                roles=["admin"],
                db=self.db,
            )
            response = list_organization_users(self.org.id, request, db=self.db)

        body = response.body.decode()

        self.assertIn("Workspace Members", body)
        self.assertIn("Quick Invite", body)
        self.assertIn("Pending Invitations", body)
        self.assertIn("Bulk Invite", body)
        self.assertIn("Active Members", body)
        self.assertIn("pending@example.com", body)
        self.assertIn("/invite/", body)

    def test_json_user_list_still_available_for_api_clients(self):
        request = self._request(accept="application/json")
        with patch("bidlens.routes.admin._current_org_or_404", return_value=(self.admin, self.org)):
            rows = list_organization_users(self.org.id, request, db=self.db)

        self.assertEqual(rows[0]["email"], "admin@members.test")
        self.assertEqual(rows[0]["role"], "admin")


if __name__ == "__main__":
    unittest.main()
