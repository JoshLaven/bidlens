import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import Organization, OrganizationMembership, SalesforceOAuthState, User
from bidlens.routes import api as api_routes
from bidlens.services.integration_credentials import encrypt_credentials
import hashlib
from datetime import datetime, timedelta


class SalesforceOnboardingRedirectTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Salesforce Setup Org", slug="salesforce-setup-org")
        self.db.add(self.org)
        self.db.flush()
        self.admin = User(email="admin@salesforce-setup.test", organization_id=self.org.id)
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
        setattr(self.admin, "current_organization_is_live", False)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _request(self):
        return SimpleNamespace(
            url_for=lambda name: "http://testserver/api/salesforce/oauth/callback",
        )

    def test_pre_live_salesforce_oauth_completion_returns_to_organization_setup(self):
        self._oauth_state("/organization-setup?org_id=%s" % self.org.id)
        service = Mock()

        with (
            patch("bidlens.routes.api.SalesforceService", return_value=service),
            patch("bidlens.routes.api.get_current_user", return_value=self.admin),
        ):
            response = api_routes.salesforce_oauth_callback(
                self._request(),
                code="auth-code",
                state="state-token",
                db=self.db,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], f"/organization-setup?org_id={self.org.id}")
        service.exchange_authorization_code.assert_called_once()

    def test_live_salesforce_oauth_completion_preserves_success_page(self):
        self.org.is_live = True
        self.db.commit()
        self._oauth_state(f"/workspace-management/business-systems/salesforce?org_id={self.org.id}&connected=1")
        service = Mock()

        with (
            patch("bidlens.routes.api.SalesforceService", return_value=service),
            patch("bidlens.routes.api.get_current_user", return_value=self.admin),
        ):
            response = api_routes.salesforce_oauth_callback(
                self._request(),
                code="auth-code",
                state="state-token",
                db=self.db,
            )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/workspace-management/business-systems/salesforce", response.headers["location"])
        service.exchange_authorization_code.assert_called_once()

    def _oauth_state(self, return_path):
        self.db.add(SalesforceOAuthState(
            state_digest=hashlib.sha256(b"state-token").hexdigest(),
            encrypted_code_verifier=encrypt_credentials({"verifier": "verifier"}),
            workspace_id=self.org.id,
            user_id=self.admin.id,
            return_path=return_path,
            expires_at=datetime.utcnow() + timedelta(minutes=10),
        ))
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
