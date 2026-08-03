import datetime as dt
import json
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity, OpportunityCommunicationMessage, OpportunityConversation,
    OpportunityNote, Organization, OrganizationMembership, User, Workspace,
)
from bidlens.services.communication_content import (
    clean_message_body, non_substantive_message_reason,
)
from bidlens.services.organizational_evidence import (
    OrganizationalEvidenceScopeError, StoredCommunicationEvidenceCollector,
    StoredNoteEvidenceCollector, combine_team_summary_evidence,
)
from bidlens.services.organizational_evidence_contracts import (
    OrganizationalEvidenceAuthor, OrganizationalEvidenceCollection,
    OrganizationalEvidenceItem, OrganizationalEvidenceSelectionPolicy,
    fingerprint_team_summary_evidence,
)


UTC = dt.timezone.utc


class TeamSummaryEvidenceContractTests(unittest.TestCase):
    def test_valid_items_actor_kinds_and_deterministic_serialization(self):
        internal = OrganizationalEvidenceAuthor(
            kind="internal_user", user_id=7, display_name="Alex", address="alex@example.com",
        )
        external = OrganizationalEvidenceAuthor(
            kind="external_person", display_name="Pat", address="pat@outside.test",
        )
        authorless = OrganizationalEvidenceAuthor(kind="authorless")
        communication = OrganizationalEvidenceItem(
            source_id="communication:2", source_type="communication",
            occurred_at=dt.datetime(2026, 8, 1, tzinfo=UTC), updated_at=None,
            text="A message", content_hash="a" * 64, author=external,
            direction="inbound", recipients=("alex@example.com",),
            original_character_count=9,
        )
        note = OrganizationalEvidenceItem(
            source_id="opportunity_note:3", source_type="note",
            occurred_at=None, updated_at=None, text="A note", content_hash="b" * 64,
            author=internal, original_character_count=6,
        )
        self.assertEqual(authorless.serializable_dict()["kind"], "authorless")
        self.assertEqual(communication.source_type, "communication")
        self.assertEqual(note.source_type, "note")
        self.assertEqual(note.canonical_json(), note.canonical_json())
        self.assertEqual(json.loads(note.canonical_json())["author"]["user_id"], 7)

    def test_invalid_actor_and_item_contracts_fail_closed(self):
        with self.assertRaises(ValueError):
            OrganizationalEvidenceAuthor(kind="internal_user", user_id=1)
        with self.assertRaises(ValueError):
            OrganizationalEvidenceAuthor(kind="authorless", display_name="Unknown")
        with self.assertRaises(ValueError):
            OrganizationalEvidenceItem(
                source_id="x", source_type="official", occurred_at=None, updated_at=None,
                text="x", content_hash="x", author=OrganizationalEvidenceAuthor(kind="authorless"),
                original_character_count=1,
            )

    def test_cleanup_preserves_existing_behavior_and_exposes_separate_filtering(self):
        self.assertEqual(clean_message_body("<div>Hello</div><div>team</div>", "html"), "Hello team")
        self.assertEqual(clean_message_body("New text\nOn Monday Pat wrote:\nold"), "New text")
        self.assertEqual(clean_message_body("Update\nRegards,\nAlex"), "Update")
        self.assertEqual(clean_message_body("Update\nSent from my iPhone"), "Update")
        self.assertEqual(clean_message_body(None), "")
        self.assertEqual(
            non_substantive_message_reason(cleaned_body="Thanks", subject="Re: update"),
            "acknowledgment_only",
        )
        self.assertEqual(
            non_substantive_message_reason(
                cleaned_body="Delivery to person has failed", subject="Delivery status notification",
            ),
            "automated_message",
        )


class TeamSummaryEvidenceCollectorTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="Org", slug="team-summary-org")
        self.other_org = Organization(name="Other", slug="team-summary-other")
        self.db.add_all((self.org, self.other_org)); self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="Workspace", slug="team-summary-workspace")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="team-summary-other-workspace")
        self.internal = User(email="Member@Example.com", name="Member Name", organization_id=self.org.id)
        self.db.add_all((self.workspace, self.other_workspace, self.internal)); self.db.flush()
        self.db.add(OrganizationMembership(
            organization_id=self.org.id, user_id=self.internal.id, role="member",
        ))
        self.opp = self._opportunity(self.org.id, "one")
        self.other_opp = self._opportunity(self.other_org.id, "two")
        self.db.add_all((self.opp, self.other_opp)); self.db.flush()
        self.conversation = OpportunityConversation(
            workspace_id=self.workspace.id, opportunity_id=self.opp.id,
            provider="microsoft", subject="Discussion",
        )
        self.db.add(self.conversation); self.db.flush(); self.db.commit()
        self.communication_policy = OrganizationalEvidenceSelectionPolicy(
            maximum_count=10, maximum_item_characters=500, maximum_total_characters=5000,
            version="comm-test-v1",
        )
        self.note_policy = OrganizationalEvidenceSelectionPolicy(
            maximum_count=10, maximum_item_characters=500, maximum_total_characters=5000,
            version="note-test-v1",
        )

    def tearDown(self):
        self.db.close(); self.engine.dispose()

    @staticmethod
    def _opportunity(org_id, key):
        return Opportunity(
            organization_id=org_id, source="manual", source_record_id=f"team-{org_id}-{key}",
            title=key, agency="Agency", opportunity_type="RFP",
            posted_date=dt.date(2026, 8, 1), response_deadline=dt.date(2026, 9, 1),
            qualification_status="qualified",
        )

    def _message(
        self, minute, body, *, sender="outside@example.test", sender_name="Outside Person",
        internet_id=None, provider_id=None, timestamp=None,
    ):
        row = OpportunityCommunicationMessage(
            workspace_id=self.workspace.id, opportunity_id=self.opp.id,
            conversation_id=self.conversation.id, associated_user_id=self.internal.id,
            provider="microsoft", direction="inbound", provider_mailbox_id="mailbox",
            provider_message_id=provider_id or f"provider-{minute}",
            provider_conversation_id="conversation", internet_message_id=internet_id,
            sender_address=sender, sender_display_name=sender_name,
            recipients_json=[{"address": "recipient@example.test"}], subject="Subject",
            body=body, body_content_type="text",
            provider_timestamp=timestamp or dt.datetime(2026, 8, 1, 12, minute),
        )
        self.db.add(row); self.db.flush(); return row

    def _note(self, minute, body, *, user=True):
        row = OpportunityNote(
            org_id=self.org.id, opportunity_id=self.opp.id,
            user_id=self.internal.id if user else None, body=body,
            created_at=dt.datetime(2026, 8, 1, 13, minute),
            updated_at=dt.datetime(2026, 8, 2, 13, minute),
        )
        self.db.add(row); self.db.flush(); return row

    def _communications(self, **kwargs):
        return StoredCommunicationEvidenceCollector(
            self.db, policy=self.communication_policy, **kwargs,
        ).collect(
            opportunity_id=self.opp.id, organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )

    def _notes(self, **kwargs):
        return StoredNoteEvidenceCollector(
            self.db, policy=self.note_policy, **kwargs,
        ).collect(
            opportunity_id=self.opp.id, organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )

    def test_communication_scope_deduplication_resolution_and_ordering(self):
        first = self._message(1, "External update", internet_id="same")
        self._message(2, "Duplicate identifier", internet_id="same")
        self._message(3, "External update", sender="outside@example.test")
        internal = self._message(
            4, "Internal update", sender="member@example.com", sender_name="Mailbox Name",
        )
        unknown = self._message(5, "Unknown author", sender=None, sender_name=None)
        result = self._communications()
        self.assertEqual([item.source_id for item in result.items], [
            f"communication:{first.id}", f"communication:{internal.id}",
            f"communication:{unknown.id}",
        ])
        self.assertEqual(result.items[0].author.kind, "external_person")
        self.assertEqual(result.items[1].author.kind, "internal_user")
        self.assertEqual(result.items[1].author.user_id, self.internal.id)
        self.assertEqual(result.items[2].author.kind, "authorless")
        self.assertEqual(result.omitted_reason_counts["duplicate_message"], 1)
        self.assertEqual(result.omitted_reason_counts["duplicate_content"], 1)
        with self.assertRaises(OrganizationalEvidenceScopeError):
            StoredCommunicationEvidenceCollector(self.db, policy=self.communication_policy).collect(
                opportunity_id=self.opp.id, organization_id=self.org.id,
                workspace_id=self.other_workspace.id,
            )
        with self.assertRaises(OrganizationalEvidenceScopeError):
            StoredCommunicationEvidenceCollector(self.db, policy=self.communication_policy).collect(
                opportunity_id=self.other_opp.id, organization_id=self.org.id,
                workspace_id=self.workspace.id,
            )

    def test_communication_filtering_and_bounding_are_policy_controlled(self):
        self._message(1, "Thanks")
        rows = [self._message(index, f"message {index} " * 10) for index in range(2, 7)]
        bounded = OrganizationalEvidenceSelectionPolicy(
            maximum_count=3, maximum_item_characters=60, maximum_total_characters=180,
            version="bounded-v1",
        )
        result = StoredCommunicationEvidenceCollector(self.db, policy=bounded).collect(
            opportunity_id=self.opp.id, organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )
        self.assertEqual(result.omitted_reason_counts["acknowledgment_only"], 1)
        self.assertEqual(result.selected_count, 3)
        self.assertEqual(result.items[0].source_id, f"communication:{rows[0].id}")
        self.assertEqual(result.items[-1].source_id, f"communication:{rows[-1].id}")
        self.assertTrue(result.truncated)

    def test_note_author_deduplication_timestamps_ordering_and_scope(self):
        first = self._note(1, "Recorded recommendation")
        self._note(2, "Recorded recommendation")
        authorless = self._note(3, "Authorless context", user=False)
        result = self._notes()
        self.assertEqual([item.source_id for item in result.items], [
            f"opportunity_note:{first.id}", f"opportunity_note:{authorless.id}",
        ])
        self.assertEqual(result.items[0].author.kind, "internal_user")
        self.assertEqual(result.items[0].updated_at, first.updated_at)
        self.assertEqual(result.items[1].author.kind, "authorless")
        self.assertEqual(result.omitted_reason_counts["duplicate_content"], 1)
        with self.assertRaises(OrganizationalEvidenceScopeError):
            StoredNoteEvidenceCollector(self.db, policy=self.note_policy).collect(
                opportunity_id=self.opp.id, organization_id=self.org.id,
                workspace_id=self.other_workspace.id,
            )

    def test_note_bounding_preserves_earliest_latest_and_is_deterministic(self):
        rows = [self._note(index, f"note {index} " * 12) for index in range(1, 7)]
        bounded = OrganizationalEvidenceSelectionPolicy(
            maximum_count=3, maximum_item_characters=50, maximum_total_characters=150,
            version="note-bounded-v1",
        )
        collector = StoredNoteEvidenceCollector(self.db, policy=bounded)
        first = collector.collect(
            opportunity_id=self.opp.id, organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )
        second = collector.collect(
            opportunity_id=self.opp.id, organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )
        self.assertEqual(first, second)
        self.assertEqual(first.selected_count, 3)
        self.assertEqual(first.items[0].source_id, f"opportunity_note:{rows[0].id}")
        self.assertEqual(first.items[-1].source_id, f"opportunity_note:{rows[-1].id}")
        self.assertTrue(first.truncated)

    def test_cross_source_rules_and_stable_tie_breaking(self):
        stamp = dt.datetime(2026, 8, 1, 12, 0)
        same_author = OrganizationalEvidenceAuthor(
            kind="internal_user", user_id=1, display_name="Alex", address="alex@example.test",
        )
        other_author = OrganizationalEvidenceAuthor(
            kind="external_person", display_name="Other", address="other@example.test",
        )
        def item(source_id, source_type, text, author):
            return OrganizationalEvidenceItem(
                source_id=source_id, source_type=source_type, occurred_at=stamp,
                updated_at=stamp, text=text, content_hash=__import__("hashlib").sha256(text.encode()).hexdigest(),
                author=author, original_character_count=len(text),
            )
        communication_items = (
            item("communication:1", "communication", "Exact content", same_author),
            item("communication:2", "communication", "Exact content", other_author),
            item("communication:3", "communication", "Similar content!", same_author),
        )
        note_items = (item("opportunity_note:1", "note", "Exact content", same_author),)
        communications = OrganizationalEvidenceCollection(
            communication_items, 3, 3, {}, sum(len(x.text) for x in communication_items), False,
        )
        notes = OrganizationalEvidenceCollection(note_items, 1, 1, {}, len(note_items[0].text), False)
        bundle = combine_team_summary_evidence(
            organization_id=1, workspace_id=2, opportunity_id=3,
            communications=communications, notes=notes,
            communication_policy=self.communication_policy, note_policy=self.note_policy,
        )
        self.assertEqual([item.source_id for item in bundle.items], [
            "opportunity_note:1", "communication:2", "communication:3",
        ])
        self.assertEqual(bundle.selected_counts, {"communication": 2, "note": 1})

    def test_fingerprint_is_content_free_deterministic_and_sensitive_to_changes(self):
        message = self._message(1, "Sensitive source body", sender="secret@example.test")
        note = self._note(2, "Private note body")
        communications = self._communications()
        notes = self._notes()
        first = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=communications, notes=notes,
            communication_policy=self.communication_policy, note_policy=self.note_policy,
        )
        second = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=self.communication_policy, note_policy=self.note_policy,
        )
        self.assertEqual(first.evidence_fingerprint, second.evidence_fingerprint)
        payload = json.dumps(first.safe_fingerprint_payload(), sort_keys=True)
        for secret in ("Sensitive source body", "Private note body", "secret@example.test"):
            self.assertNotIn(secret, payload)

        note.body = "Edited private note"; self.db.commit()
        edited = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=self.communication_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(first.evidence_fingerprint, edited.evidence_fingerprint)

        self.db.delete(note); self.db.commit()
        deleted = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=self.communication_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(edited.evidence_fingerprint, deleted.evidence_fingerprint)

        changed_policy = OrganizationalEvidenceSelectionPolicy(
            maximum_count=10, maximum_item_characters=500, maximum_total_characters=5000,
            version="comm-test-v2",
        )
        changed = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=changed_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(deleted.evidence_fingerprint, changed.evidence_fingerprint)

        message.sender_address = "changed@example.test"; self.db.commit()
        author_changed = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=changed_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(changed.evidence_fingerprint, author_changed.evidence_fingerprint)

        message.body = "Edited communication body"; self.db.commit()
        communication_edited = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=changed_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(author_changed.evidence_fingerprint, communication_edited.evidence_fingerprint)

        added = self._message(3, "New organizational evidence")
        self.db.commit()
        source_added = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=changed_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(communication_edited.evidence_fingerprint, source_added.evidence_fingerprint)

        added.provider_timestamp = dt.datetime(2026, 7, 31, 8, 0); self.db.commit()
        reordered = combine_team_summary_evidence(
            organization_id=self.org.id, workspace_id=self.workspace.id,
            opportunity_id=self.opp.id, communications=self._communications(), notes=self._notes(),
            communication_policy=changed_policy, note_policy=self.note_policy,
        )
        self.assertNotEqual(source_added.evidence_fingerprint, reordered.evidence_fingerprint)


if __name__ == "__main__":
    unittest.main()
