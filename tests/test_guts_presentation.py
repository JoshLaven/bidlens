import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from bidlens.routes.api import generate_guts
from bidlens.services.opportunity_knowledge_brief import GUTSServiceError
from bidlens.services.opportunity_knowledge_brief.presentation import build_guts_presentation


def statement(
    key, text, *, placement="section", section=None, position=0, source_id=None,
    importance="normal", occurred_at=None, persisted_id=None, attribution_json=None,
):
    source = SimpleNamespace(
        source_id=source_id or f"source:{key}",
        citation_label=f"Citation {key}",
        source_class="current_state" if section == "current_state" else "official_evidence",
        source_type="test",
        occurred_at=occurred_at,
        effective_at=None,
        updated_at_source=None,
    )
    return SimpleNamespace(
        id=persisted_id or position + 1,
        statement_key=key,
        text=text,
        placement_type=placement,
        section_type=section,
        position=position,
        confidence="supported",
        importance=importance,
        attribution_json=attribution_json,
        source_links=[SimpleNamespace(brief_source=source)],
    )


class GUTSPresentationTests(unittest.TestCase):
    def test_maps_only_v1_sections_without_rewriting_and_preserves_traceability(self):
        rows = [
            statement(
                "headline", "The opportunity remains active.", placement="headline",
                source_id="current_state:opportunity:1:source_stage", persisted_id=41,
            ),
            statement(
                "current-1", "The deadline is August 31.", section="current_state", position=1,
                source_id="current_state:opportunity:1:response_deadline",
            ),
            statement("history-1", "Amendment 3 was released.", section="important_history", position=2),
            statement(
                "org-1", "Josh recommended contacting Westat.",
                section="organizational_knowledge", position=3,
                occurred_at=datetime(2026, 7, 31, 18, 30, tzinfo=timezone.utc),
            ),
            statement("risk-1", "Staffing availability is unresolved.", section="uncertainties", position=4),
        ]

        result = build_guts_presentation(SimpleNamespace(statements=rows))

        self.assertEqual(result.overall_status, ())
        self.assertEqual(result.recent_developments[0].text, "Amendment 3 was released.")
        self.assertEqual(result.internal_activity[0].text, "Josh recommended contacting Westat.")
        self.assertFalse(hasattr(result, "risks_watch_items"))
        self.assertFalse(hasattr(result, "suggested_next_steps"))
        self.assertEqual(result.internal_activity[0].statement_key, "org-1")
        self.assertEqual(result.internal_activity[0].citations[0].source_id, "source:org-1")
        self.assertEqual(result.internal_activity[0].confidence, "supported")
        self.assertEqual(result.internal_activity[0].display_date, "Jul 31")

    def test_overall_status_is_not_exposed_by_the_dynamic_brief(self):
        rows = [
            statement("title", "Evaluation Services.", placement="headline", source_id="current_state:opportunity:1:title"),
            statement("description", "The agency seeks broad program support.", placement="summary", position=4, source_id="current_state:opportunity:1:description"),
            statement("stage", "The opportunity remains active.", placement="summary", position=1, source_id="current_state:opportunity:1:source_stage"),
            statement("deadline", "Responses are due August 31.", section="current_state", position=2, source_id="current_state:opportunity:1:response_deadline"),
            statement("type", "This is a request for proposals.", section="current_state", position=3, source_id="current_state:opportunity:1:opportunity_type"),
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual(result.overall_status, ())

    def test_recent_developments_uses_all_history_newest_first(self):
        rows = [
            statement("static-official", "The award ceiling is $2 million.", section="official_updates"),
            *[
                statement(
                    f"history-{index}", f"Historical change {index}.", section="important_history",
                    position=index, occurred_at=datetime(2026, 7, index, tzinfo=timezone.utc),
                )
                for index in range(1, 5)
            ],
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual(
            [item.statement_key for item in result.recent_developments],
            ["history-4", "history-3", "history-2", "history-1"],
        )
        self.assertNotIn("static-official", [item.statement_key for item in result.recent_developments])

    def test_internal_activity_preserves_all_canonical_statements_without_ranking(self):
        rows = [
            statement("normal-1", "Josh noted prior experience.", section="organizational_knowledge", position=1),
            statement("high-2", "Kendall recommended a partner.", section="organizational_knowledge", position=2, importance="high"),
            statement("high-3", "Tom raised a staffing question.", section="organizational_knowledge", position=3, importance="high"),
            statement("normal-4", "Ana shared research.", section="organizational_knowledge", position=4),
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual(
            [item.statement_key for item in result.internal_activity],
            ["normal-1", "high-2", "high-3", "normal-4"],
        )
        self.assertEqual(
            [item.text for item in result.internal_activity],
            [
                "Josh noted prior experience.",
                "Kendall recommended a partner.",
                "Tom raised a staffing question.",
                "Ana shared research.",
            ],
        )

    def test_long_internal_activity_is_not_truncated(self):
        rows = [
            statement(
                f"org-{index}", f"Person {index} shared organizational context.",
                section="organizational_knowledge", position=index,
            )
            for index in range(1, 13)
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual(len(result.internal_activity), 12)
        self.assertEqual(
            [item.statement_key for item in result.internal_activity],
            [f"org-{index}" for index in range(1, 13)],
        )

    def test_internal_activity_uses_latest_existing_evidence_date_with_consistent_format(self):
        activity = statement(
            "org-dated",
            "Josh noted previous relevant work.",
            section="organizational_knowledge",
            occurred_at=datetime(2026, 8, 1, 9, 15),
        )
        result = build_guts_presentation(SimpleNamespace(statements=[activity]))
        self.assertEqual(result.internal_activity[0].display_date, "Aug 1")
        self.assertEqual(result.internal_activity[0].text, activity.text)
        self.assertEqual(result.internal_activity[0].citations[0].occurred_at, activity.source_links[0].brief_source.occurred_at)

    def test_v2_attribution_is_retained_without_rewriting_or_exposing_email(self):
        activity = statement(
            "org-attributed", "Josh Laven recommended contacting Westat.",
            section="organizational_knowledge", occurred_at=datetime(2026, 8, 1, 9, 15),
            attribution_json={
                "type": "person",
                "actors": [{
                    "user_id": 4, "display_name": "Josh Laven",
                    "email": "private@example.test",
                }],
            },
        )
        result = build_guts_presentation(SimpleNamespace(statements=[activity]))
        presented = result.internal_activity[0]
        self.assertEqual(presented.text, activity.text)
        self.assertEqual(presented.display_date, "Aug 1")
        self.assertEqual(presented.citations[0].source_id, "source:org-attributed")
        self.assertEqual(presented.attribution, {
            "type": "person",
            "actors": [{"user_id": 4, "display_name": "Josh Laven"}],
        })
        self.assertNotIn("email", presented.attribution["actors"][0])

    def test_missing_generation_has_only_empty_v1_sections(self):
        result = build_guts_presentation(None)
        self.assertEqual(result.overall_status, ())
        self.assertEqual(result.recent_developments, ())
        self.assertEqual(result.internal_activity, ())


class GUTSGenerationEndpointTests(unittest.TestCase):
    def test_endpoint_uses_existing_service_lifecycle(self):
        user = SimpleNamespace(id=4, organization_id=9, current_organization_id=9)
        service = MagicMock()
        service.generate.return_value = SimpleNamespace(id=21, status="succeeded")
        with (
            patch("bidlens.routes.api.require_user", return_value=user),
            patch("bidlens.routes.api.OpportunityKnowledgeBriefService", return_value=service),
        ):
            response = generate_guts(180, MagicMock(), MagicMock())

        self.assertEqual(response, {"ok": True, "generation_id": 21, "status": "succeeded"})
        service.generate.assert_called_once_with(
            opportunity_id=180,
            requesting_user=user,
            active_organization_id=9,
        )

    def test_endpoint_returns_only_safe_service_failure(self):
        user = SimpleNamespace(id=4, organization_id=9, current_organization_id=9)
        service = MagicMock()
        service.generate.side_effect = GUTSServiceError(
            "shortlist_required",
            "Add this opportunity to your shortlist before generating a briefing.",
            stage="authorization",
        )
        with (
            patch("bidlens.routes.api.require_user", return_value=user),
            patch("bidlens.routes.api.OpportunityKnowledgeBriefService", return_value=service),
        ):
            with self.assertRaises(HTTPException) as raised:
                generate_guts(180, MagicMock(), MagicMock())

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail["code"], "shortlist_required")
        self.assertNotIn("stage", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
