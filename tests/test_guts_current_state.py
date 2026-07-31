import unittest
from datetime import date, datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from bidlens.database import Base
from bidlens.models import (
    Opportunity,
    OpportunityBrief,
    OpportunityOutcome,
    Organization,
    OrganizationMembership,
    User,
    Vote,
    Workspace,
)
from bidlens.services.opportunity_knowledge_brief import CurrentStateAssembler, CurrentStateScopeError


class GutsCurrentStateTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.org = Organization(name="State", slug="guts-state")
        self.other_org = Organization(name="Other", slug="guts-state-other")
        self.db.add_all([self.org, self.other_org])
        self.db.flush()
        self.workspace = Workspace(organization_id=self.org.id, name="State", slug="guts-state")
        self.other_workspace = Workspace(organization_id=self.other_org.id, name="Other", slug="guts-state-other")
        self.alice = User(email="alice@example.test", name="Alice", organization_id=self.org.id)
        self.bob = User(email="bob@example.test", name=None, organization_id=self.org.id)
        self.charlie = User(email="charlie@example.test", name="Charlie", organization_id=self.org.id)
        self.db.add_all([self.workspace, self.other_workspace, self.alice, self.bob, self.charlie])
        self.db.flush()
        self.db.add_all([
            OrganizationMembership(organization_id=self.org.id, user_id=user.id, role="member")
            for user in (self.alice, self.bob, self.charlie)
        ])
        self.opportunity = Opportunity(
            organization_id=self.org.id,
            source="sam",
            source_record_id="notice-554",
            solicitation_number="RFP-554",
            source_url="https://sam.gov/source/554",
            title="Health Evaluation",
            agency="Department of Health",
            opportunity_type="RFP",
            source_stage="Solicitation",
            posted_date=date(2026, 7, 1),
            response_deadline=date(2026, 9, 1),
            description_text="<p>Preferred synopsis</p><p>Second paragraph</p>",
            description="Fallback description",
            raw_source_payload={"secret": "must not appear"},
            sam_url="https://sam.gov/opp/554/view",
            sam_notice_id="notice-554",
            naics="541611",
            naics_title="Administrative Management Consulting Services",
            set_aside="Small Business",
            qualification_status="qualified",
            salesforce_opportunity_id="006ABC",
            salesforce_opportunity_url="https://salesforce.example/006ABC",
        )
        self.db.add(self.opportunity)
        self.db.flush()
        self.db.add_all([
            Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.charlie.id, vote="PASS"),
            Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.bob.id, vote="PURSUE"),
            Vote(org_id=self.org.id, opp_id=self.opportunity.id, user_id=self.alice.id, vote="PURSUE"),
        ])
        self.db.add(OpportunityOutcome(
            organization_id=self.org.id,
            opportunity_id=self.opportunity.id,
            outcome_type="bidding",
            recorded_by=self.alice.id,
            recorded_at=datetime(2026, 7, 31, 12, tzinfo=timezone.utc),
            reason_code="internal_reason",
            reason_text="Do not include this reason",
            notes="Do not include these notes",
        ))
        self.db.add(OpportunityBrief(
            organization_id=self.org.id,
            opportunity_id=self.opportunity.id,
            brief_json={"previous_ai_output": "must not appear"},
            status="completed",
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_exact_mapping_description_precedence_outcome_interest_and_salesforce(self):
        state = CurrentStateAssembler(self.db).build(
            opportunity=self.opportunity,
            organization_id=self.org.id,
            workspace_id=self.workspace.id,
        )
        self.assertEqual(state.title.value, "Health Evaluation")
        self.assertEqual(state.client.value, "Department of Health")
        self.assertEqual(state.description.value, "Preferred synopsis\n\nSecond paragraph")
        self.assertEqual(state.response_deadline.value, "2026-09-01")
        self.assertEqual(state.posted_date.value, "2026-07-01")
        self.assertEqual(state.solicitation_number.value, "RFP-554")
        self.assertEqual(state.source.value, "sam")
        self.assertEqual(state.source_record_id.value, "notice-554")
        self.assertEqual(state.naics.value, "541611")
        self.assertEqual(state.outcome.outcome_type, "bidding")
        self.assertEqual(state.outcome.recorded_by_display_name, "Alice")
        self.assertEqual([item.display_name for item in state.interested_teammates], ["Alice", "bob@example.test"])
        self.assertNotIn("Charlie", [item.display_name for item in state.interested_teammates])
        self.assertTrue(state.salesforce.linked)
        self.assertEqual(state.salesforce.url, "https://salesforce.example/006ABC")
        self.assertEqual(
            state.response_deadline.source_id,
            f"current_state:opportunity:{self.opportunity.id}:response_deadline",
        )

    def test_description_fallback_bounding_and_null_behavior(self):
        self.opportunity.description_text = None
        self.opportunity.description = "Fallback " * 20
        self.opportunity.salesforce_opportunity_id = None
        self.opportunity.salesforce_opportunity_url = None
        state = CurrentStateAssembler(self.db, description_max_characters=30).build(
            opportunity=self.opportunity, organization_id=self.org.id, workspace_id=self.workspace.id,
        )
        self.assertTrue(state.description.value.startswith("Fallback"))
        self.assertLessEqual(len(state.description.value), 30)
        self.assertTrue(state.description_was_truncated)
        self.assertFalse(state.salesforce.linked)
        self.assertIsNone(state.salesforce.url)
        self.db.expunge(self.opportunity)
        self.opportunity.description = "https://example.test/description"
        self.opportunity.response_deadline = None
        state = CurrentStateAssembler(self.db).build(
            opportunity=self.opportunity, organization_id=self.org.id, workspace_id=self.workspace.id,
        )
        self.assertIsNone(state.description.value)
        self.assertIsNone(state.response_deadline.value)

    def test_no_outcome_is_unknown_and_not_inferred_from_votes(self):
        self.db.query(OpportunityOutcome).delete()
        self.db.commit()
        state = CurrentStateAssembler(self.db).build(
            opportunity=self.opportunity, organization_id=self.org.id, workspace_id=self.workspace.id,
        )
        self.assertIsNone(state.outcome)
        self.assertEqual(len(state.interested_teammates), 2)

    def test_compact_snapshot_excludes_raw_technical_and_prior_ai_data(self):
        assembler = CurrentStateAssembler(self.db)
        snapshot = assembler.compact_snapshot(assembler.build(
            opportunity=self.opportunity, organization_id=self.org.id, workspace_id=self.workspace.id,
        ))
        rendered = str(snapshot)
        for excluded in (
            "raw_source_payload", "must not appear", "upserted_at", "last_seen_at",
            "reason_code", "internal_reason", "Do not include", "previous_ai_output",
        ):
            self.assertNotIn(excluded, rendered)
        self.assertEqual(snapshot["fields"]["client"], "Department of Health")

    def test_assembler_queries_no_notes_messages_history_or_prior_brief(self):
        statements = []

        def before_cursor_execute(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement.lower())

        event.listen(self.engine, "before_cursor_execute", before_cursor_execute)
        try:
            CurrentStateAssembler(self.db).build(
                opportunity=self.opportunity, organization_id=self.org.id, workspace_id=self.workspace.id,
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", before_cursor_execute)
        sql = "\n".join(statements)
        for forbidden_table in (
            "opportunity_notes", "opportunity_communication_messages", "opportunity_history_events",
            "opportunity_update_events", "opportunity_briefs", "opportunity_communication_summaries",
        ):
            self.assertNotIn(forbidden_table, sql)

    def test_scope_mismatch_is_rejected(self):
        assembler = CurrentStateAssembler(self.db)
        with self.assertRaises(CurrentStateScopeError):
            assembler.build(
                opportunity=self.opportunity, organization_id=self.other_org.id,
                workspace_id=self.workspace.id,
            )
        with self.assertRaises(CurrentStateScopeError):
            assembler.build(
                opportunity=self.opportunity, organization_id=self.org.id,
                workspace_id=self.other_workspace.id,
            )


if __name__ == "__main__":
    unittest.main()
