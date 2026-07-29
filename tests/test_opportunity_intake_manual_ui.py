import re
import io
import tempfile
import unittest
import zipfile
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from bidlens import auth, config, main
from bidlens.database import Base, get_db
from bidlens.models import (
    Opportunity,
    OpportunityIntakeDraft,
    Organization,
    OrganizationMembership,
    User,
    Vote,
    Workspace,
)
from bidlens.services.opportunity_intake import (
    LocalSourceMaterialStorage,
    create_draft,
    parse_extraction_payload,
    parse_email_extraction_payload,
)


def _docx_bytes(text="Department of Health requests evaluation services"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>',
        )
        archive.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + text
            + "</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


def _extracted_document():
    return parse_extraction_payload(
        {
            "title": {"value": "Extracted Evaluation", "confidence": "high", "evidence": "Title"},
            "client": {"value": "Department of Health", "confidence": "high", "evidence": "Client"},
            "response_deadline": {"value": "2026-10-30", "confidence": "high", "evidence": "Deadline"},
            "solicitation_number": {"value": "RFP-42", "confidence": "high", "evidence": "Number"},
            "opportunity_type": {"value": "RFP", "confidence": "high", "evidence": "Type"},
            "description": {"value": "Evaluation services.", "confidence": "high", "evidence": "Scope"},
            "warnings": [],
        },
        model="test-model",
    )


def _email_bytes(subject="Email Intake Opportunity"):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "sender@example.test"
    message["To"] = "reviewer@example.test"
    message["Message-ID"] = "<ui-email@example.test>"
    message.set_content("The Department of Health requests evaluation proposals due October 30, 2026.")
    return message.as_bytes()


def _extracted_email():
    return parse_email_extraction_payload(
        {
            "title": {"value": "Email Intake Opportunity", "confidence": "high", "evidence": "Subject", "source": "email"},
            "client": {"value": "Department of Health", "confidence": "high", "evidence": "Body", "source": "email"},
            "response_deadline": {"value": "2026-10-30", "confidence": "high", "evidence": "Body", "source": "email"},
            "solicitation_number": {"value": None, "confidence": "unknown", "evidence": None, "source": "unknown"},
            "opportunity_type": {"value": "RFP", "confidence": "high", "evidence": "Body", "source": "email"},
            "description": {"value": "Evaluation proposals requested.", "confidence": "high", "evidence": "Body", "source": "email"},
            "warnings": [],
        },
        model="test-model",
    )


class ManualOpportunityIntakeUiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.org = Organization(name="Manual Intake Org", slug="manual-intake-org", is_live=True)
        self.other_org = Organization(name="Other Manual Org", slug="other-manual-org", is_live=True)
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Manual", slug="manual-intake")
        self.other_workspace = Workspace(
            organization_id=self.other_org.id, name="Other Manual", slug="other-manual-intake"
        )
        self.member = User(email="member@manual.test", organization_id=self.org.id)
        self.colleague = User(email="colleague@manual.test", organization_id=self.org.id)
        self.outsider = User(email="outsider@other.test", organization_id=self.other_org.id)
        self.nonmember = User(email="nonmember@unmatched.test", organization_id=self.org.id)
        self.db.add_all([
            self.workspace, self.other_workspace, self.member, self.colleague, self.outsider,
            self.nonmember,
        ])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=self.member.id, role="member"),
            OrganizationMembership(organization_id=self.org.id, user_id=self.colleague.id, role="member"),
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
        self._login(self.member)

    def tearDown(self):
        self.client.close()
        main.app.dependency_overrides.clear()
        self.db.close()
        self.engine.dispose()

    def _login(self, user):
        self.client.cookies.set(
            config.SESSION_COOKIE_NAME,
            auth.serializer.dumps({"user_id": user.id}),
        )

    @staticmethod
    def _token(html, action):
        match = re.search(
            rf'<form[^>]+action="{re.escape(action)}".*?<input type="hidden" name="csrf_token" value="([^"]+)"',
            html,
            re.S,
        )
        if not match:
            raise AssertionError(f"CSRF token not found for {action}")
        return match.group(1)

    @staticmethod
    def _redirect_location(response):
        if "location" in response.headers:
            return response.headers["location"]
        match = re.search(r'url=([^">]+)', response.text)
        if not match:
            raise AssertionError(f"Redirect location not found: {response.text}")
        return match.group(1).replace("&amp;", "&")

    def _start_draft(self):
        page = self.client.get("/opportunities/new")
        token = self._token(page.text, "/opportunity-intake/manual")
        response = self.client.post(
            "/opportunity-intake/manual",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (200, 303), response.text)
        location = self._redirect_location(response)
        draft_id = int(re.search(r"/opportunity-intake/(\d+)/review", location).group(1))
        return draft_id

    def _review_token(self, draft_id):
        page = self.client.get(f"/opportunity-intake/{draft_id}/review")
        return page, self._token(page.text, f"/opportunity-intake/{draft_id}/publish")

    def _valid_form(self, token, **overrides):
        values = {
            "csrf_token": token,
            "title": "Community Health Evaluation",
            "client": "Department of Health",
            "response_deadline": "2026-10-15",
            "solicitation_number": "MANUAL-2026-1",
            "opportunity_type": "RFP",
            "description": "Evaluation services are requested.",
            "add_to_shortlist": "1",
        }
        values.update(overrides)
        return values

    def test_member_can_open_start_with_all_three_intake_methods(self):
        response = self.client.get("/opportunities/new")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Create Opportunity", response.text)
        self.assertIn("Enter Manually", response.text)
        self.assertEqual(response.text.count("Coming soon"), 0)
        self.assertIn('action="/opportunity-intake/email"', response.text)
        self.assertIn('accept=".eml,message/rfc822"', response.text)
        self.assertIn('action="/opportunity-intake/document"', response.text)
        self.assertIn('accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"', response.text)
        self.assertNotIn('action="/opportunity-intake/upload"', response.text)

    def test_document_upload_uses_real_route_and_prefills_review_without_opportunity(self):
        page = self.client.get("/opportunities/new")
        token = self._token(page.text, "/opportunity-intake/document")
        with tempfile.TemporaryDirectory() as directory, patch(
            "bidlens.routes.opportunity_intake.configured_source_material_storage",
            return_value=LocalSourceMaterialStorage(Path(directory)),
        ), patch(
            "bidlens.services.opportunity_intake.document_upload.OpenAIIntakeDocumentExtractor.extract",
            return_value=_extracted_document(),
        ) as extract:
            response = self.client.post(
                "/opportunity-intake/document",
                data={"csrf_token": token},
                files={
                    "document": (
                        "request.docx",
                        _docx_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    )
                },
                follow_redirects=False,
            )
            self.assertIn(response.status_code, (200, 303), response.text)
            location = self._redirect_location(response)
            review = self.client.get(location)
            self.assertIn('value="Extracted Evaluation"', review.text)
            self.assertIn('value="Department of Health"', review.text)
            self.assertIn('value="2026-10-30"', review.text)
            self.assertEqual(extract.call_count, 1)
            self.client.get(location)
            self.assertEqual(extract.call_count, 1)
        self.assertEqual(self.db.query(Opportunity).count(), 0)
        draft = self.db.query(OpportunityIntakeDraft).one()
        self.assertEqual(draft.intake_method, "document")

    def test_invalid_document_returns_actionable_error_without_draft(self):
        page = self.client.get("/opportunities/new")
        token = self._token(page.text, "/opportunity-intake/document")
        response = self.client.post(
            "/opportunity-intake/document",
            data={"csrf_token": token},
            files={"document": ("legacy.doc", b"not-a-docx", "application/msword")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Legacy .doc files are not supported", response.text)
        self.assertEqual(self.db.query(OpportunityIntakeDraft).count(), 0)

    def test_email_upload_uses_real_route_and_review_refresh_does_not_extract_again(self):
        page = self.client.get("/opportunities/new")
        token = self._token(page.text, "/opportunity-intake/email")
        with tempfile.TemporaryDirectory() as directory, patch(
            "bidlens.routes.opportunity_intake.configured_source_material_storage",
            return_value=LocalSourceMaterialStorage(Path(directory)),
        ), patch(
            "bidlens.services.opportunity_intake.email_upload.OpenAIIntakeEmailExtractor.extract",
            return_value=_extracted_email(),
        ) as extract:
            response = self.client.post(
                "/opportunity-intake/email",
                data={"csrf_token": token, "organization_id": self.other_org.id},
                files={"email_file": ("request.eml", _email_bytes(), "message/rfc822")},
                follow_redirects=False,
            )
            location = self._redirect_location(response)
            review = self.client.get(location)
            self.assertIn('value="Email Intake Opportunity"', review.text)
            self.assertIn('value="Department of Health"', review.text)
            self.assertIn('value="2026-10-30"', review.text)
            self.assertEqual(extract.call_count, 1)
            self.client.get(location)
            self.assertEqual(extract.call_count, 1)
        draft = self.db.query(OpportunityIntakeDraft).one()
        self.assertEqual(draft.organization_id, self.org.id)
        self.assertEqual(draft.workspace_id, self.workspace.id)
        self.assertEqual(draft.intake_method, "email")
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_msg_upload_is_rejected_clearly(self):
        page = self.client.get("/opportunities/new")
        token = self._token(page.text, "/opportunity-intake/email")
        response = self.client.post(
            "/opportunity-intake/email",
            data={"csrf_token": token},
            files={"email_file": ("outlook.msg", b"msg", "application/vnd.ms-outlook")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Outlook .msg files are not supported yet", response.text)
        self.assertEqual(self.db.query(OpportunityIntakeDraft).count(), 0)

    def test_nonmember_is_denied(self):
        self._login(self.nonmember)
        response = self.client.get("/opportunities/new")
        self.assertEqual(response.status_code, 403)

    def test_start_creates_manual_draft_not_opportunity_with_checked_default(self):
        draft_id = self._start_draft()
        draft = self.db.get(OpportunityIntakeDraft, draft_id)
        self.db.refresh(draft)
        self.assertEqual(draft.intake_method, "manual")
        self.assertTrue(draft.add_to_shortlist)
        self.assertTrue(draft.publish_idempotency_key)
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_manual_intake_remains_available_when_storage_is_unavailable(self):
        with patch(
            "bidlens.routes.opportunity_intake.configured_source_material_storage",
            side_effect=RuntimeError("storage unavailable"),
        ):
            draft_id = self._start_draft()
        self.assertEqual(self.db.get(OpportunityIntakeDraft, draft_id).intake_method, "manual")

    def test_reopening_review_displays_persisted_values_and_accessible_labels(self):
        draft = create_draft(
            self.db,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
            user_id=self.member.id,
            intake_method="manual",
            candidate={
                "title": "Persisted title",
                "client": "Persisted client",
                "response_deadline": "2026-10-20",
                "solicitation_number": "LONG-IDENTIFIER-1234567890",
            },
            publish_idempotency_key="persisted-key",
        )
        self.db.commit()
        response = self.client.get(f"/opportunity-intake/{draft.id}/review")
        self.assertIn('for="intake-title"', response.text)
        self.assertIn('id="intake-title"', response.text)
        self.assertIn('value="Persisted title"', response.text)
        self.assertIn('value="Persisted client"', response.text)
        self.assertIn("BidLens will assign an internal reference", response.text)
        self.assertIn('id="intake-shortlist"', response.text)
        self.assertIn("checked", response.text)

    def test_validation_preserves_values_and_explicit_unchecked_state(self):
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        form = self._valid_form(
            token,
            title="",
            client="Entered client",
            response_deadline="not-a-date",
            solicitation_number="OPTIONAL-42",
        )
        form.pop("add_to_shortlist")
        response = self.client.post(
            f"/opportunity-intake/{draft_id}/publish",
            data=form,
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Opportunity Title is required", response.text)
        self.assertIn("Enter a valid Response Deadline", response.text)
        self.assertIn('value="Entered client"', response.text)
        self.assertIn('value="OPTIONAL-42"', response.text)
        shortlist_tag = re.search(r'<input id="intake-shortlist"[^>]+>', response.text).group(0)
        self.assertNotIn("checked", shortlist_tag)
        self.assertFalse(self.db.get(OpportunityIntakeDraft, draft_id).add_to_shortlist)
        self.assertEqual(self.db.query(Opportunity).count(), 0)

    def test_blank_solicitation_publishes_and_checked_redirects_to_detail(self):
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        response = self.client.post(
            f"/opportunity-intake/{draft_id}/publish",
            data=self._valid_form(token, solicitation_number=""),
            follow_redirects=False,
        )
        self.assertIn(response.status_code, (200, 303))
        location = self._redirect_location(response)
        self.assertRegex(location, r"/opportunity/\d+\?return_to=feed&intake_published=shortlisted")
        opportunity = self.db.query(Opportunity).one()
        self.assertEqual(opportunity.qualification_status, "qualified")
        self.assertEqual(opportunity.decision_state, "INBOX")
        self.assertTrue(opportunity.solicitation_number.startswith("BL-"))
        self.assertEqual(self.db.query(Vote).filter(Vote.vote == "PURSUE").count(), 1)
        detail = self.client.get(location)
        self.assertIn("Opportunity posted to the Feed and added to My Shortlist.", detail.text)

    def test_unchecked_publication_creates_no_vote_and_ignores_submitted_tenant_ids(self):
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        form = self._valid_form(token, solicitation_number="MANUAL-NO-VOTE")
        form.pop("add_to_shortlist")
        form.update({"organization_id": str(self.other_org.id), "workspace_id": str(self.other_workspace.id)})
        response = self.client.post(
            f"/opportunity-intake/{draft_id}/publish", data=form, follow_redirects=False
        )
        location = self._redirect_location(response)
        self.assertIn("intake_published=feed", location)
        opportunity = self.db.query(Opportunity).one()
        self.assertEqual(opportunity.organization_id, self.org.id)
        self.assertEqual(self.db.query(Vote).count(), 0)
        detail = self.client.get(location)
        self.assertIn("Opportunity posted to the Feed.", detail.text)

    def test_publish_route_uses_canonical_publisher(self):
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        with patch(
            "bidlens.routes.opportunity_intake.OpportunityPublisher.publish_reviewed_draft",
            wraps=__import__(
                "bidlens.services.opportunity_intake.publisher", fromlist=["OpportunityPublisher"]
            ).OpportunityPublisher.publish_reviewed_draft,
        ) as publish:
            response = self.client.post(
                f"/opportunity-intake/{draft_id}/publish",
                data=self._valid_form(token),
                follow_redirects=False,
            )
        self.assertIn(response.status_code, (200, 303))
        publish.assert_called_once()

    def test_double_submission_creates_one_opportunity_and_one_vote(self):
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        form = self._valid_form(token)
        first = self.client.post(
            f"/opportunity-intake/{draft_id}/publish", data=form, follow_redirects=False
        )
        second = self.client.post(
            f"/opportunity-intake/{draft_id}/publish", data=form, follow_redirects=False
        )
        self.assertIn(first.status_code, (200, 303))
        self.assertIn(second.status_code, (200, 303))
        self.assertEqual(self.db.query(Opportunity).count(), 1)
        self.assertEqual(self.db.query(Vote).count(), 1)

    def test_exact_duplicate_blocks_and_preserves_reviewed_values(self):
        self.db.add(Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="existing-manual",
            solicitation_number="MANUAL-2026-1",
            title="Existing",
            agency="Client",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 1),
            response_deadline=date(2026, 10, 15),
            qualification_status="qualified",
            decision_state="INBOX",
        ))
        self.db.commit()
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        response = self.client.post(
            f"/opportunity-intake/{draft_id}/publish", data=self._valid_form(token)
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("duplicate an opportunity", response.text)
        self.assertIn("View existing opportunity", response.text)
        self.assertIn('value="Community Health Evaluation"', response.text)
        self.assertEqual(self.db.query(Opportunity).count(), 1)

    def test_probable_duplicate_requires_confirmation_then_publishes(self):
        self.db.add(Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="probable-existing",
            solicitation_number="OTHER-1",
            title="Community Health Evaluation",
            agency="Department of Health",
            opportunity_type="RFP",
            posted_date=date(2026, 7, 1),
            response_deadline=date(2026, 10, 15),
            qualification_status="qualified",
            decision_state="INBOX",
        ))
        self.db.commit()
        draft_id = self._start_draft()
        _, token = self._review_token(draft_id)
        form = self._valid_form(token)
        warning = self.client.post(f"/opportunity-intake/{draft_id}/publish", data=form)
        self.assertEqual(warning.status_code, 200)
        self.assertIn("Possible duplicate found", warning.text)
        self.assertIn('name="confirm_probable_duplicates" value="1"', warning.text)
        next_token = self._token(warning.text, f"/opportunity-intake/{draft_id}/publish")
        confirmed = self.client.post(
            f"/opportunity-intake/{draft_id}/publish",
            data=self._valid_form(next_token, confirm_probable_duplicates="1"),
            follow_redirects=False,
        )
        self.assertIn(confirmed.status_code, (200, 303))
        self.assertEqual(self.db.query(Opportunity).count(), 2)

    def test_csrf_and_cross_creator_access_are_enforced(self):
        draft_id = self._start_draft()
        bad = self.client.post(
            f"/opportunity-intake/{draft_id}/publish",
            data=self._valid_form("bad-token"),
        )
        self.assertEqual(bad.status_code, 403)
        self._login(self.colleague)
        denied = self.client.get(f"/opportunity-intake/{draft_id}/review")
        self.assertEqual(denied.status_code, 404)
        self._login(self.outsider)
        cross_workspace = self.client.get(f"/opportunity-intake/{draft_id}/review")
        self.assertEqual(cross_workspace.status_code, 404)

    def test_feed_entry_point_is_visible_without_admin_condition(self):
        feed = Path("src/bidlens/templates/feed.html").read_text()
        self.assertIn("'/opportunities/new', '+ Create Opportunity'", feed)
        self.assertNotRegex(feed, r"if user\.current_role.*Create Opportunity")


if __name__ == "__main__":
    unittest.main()
