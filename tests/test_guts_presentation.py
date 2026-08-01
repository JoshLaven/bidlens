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
    importance="normal", occurred_at=None, persisted_id=None,
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
            statement("org-1", "Josh recommended contacting Westat.", section="organizational_knowledge", position=3),
            statement("risk-1", "Staffing availability is unresolved.", section="uncertainties", position=4),
        ]

        result = build_guts_presentation(SimpleNamespace(statements=rows))

        self.assertEqual(
            [item.text for item in result.overall_status],
            ["The opportunity remains active.", "The deadline is August 31."],
        )
        self.assertEqual(result.recent_developments[0].text, "Amendment 3 was released.")
        self.assertEqual(result.internal_activity[0].text, "Josh recommended contacting Westat.")
        self.assertFalse(hasattr(result, "risks_watch_items"))
        self.assertFalse(hasattr(result, "suggested_next_steps"))
        self.assertEqual(result.overall_status[0].persisted_statement_id, 41)
        self.assertEqual(result.internal_activity[0].statement_key, "org-1")
        self.assertEqual(result.internal_activity[0].citations[0].source_id, "source:org-1")

    def test_overall_status_prefers_distinct_stage_and_deadline_and_caps_at_two(self):
        rows = [
            statement("title", "Evaluation Services.", placement="headline", source_id="current_state:opportunity:1:title"),
            statement("stage", "The opportunity remains active.", placement="summary", position=1, source_id="current_state:opportunity:1:source_stage"),
            statement("deadline", "Responses are due August 31.", section="current_state", position=2, source_id="current_state:opportunity:1:response_deadline"),
            statement("type", "This is a request for proposals.", section="current_state", position=3, source_id="current_state:opportunity:1:opportunity_type"),
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual([item.statement_key for item in result.overall_status], ["stage", "deadline"])
        self.assertEqual(len(result.overall_status), 2)

    def test_recent_developments_uses_only_history_newest_first_and_caps_at_three(self):
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
            ["history-4", "history-3", "history-2"],
        )
        self.assertNotIn("static-official", [item.statement_key for item in result.recent_developments])

    def test_internal_activity_prefers_importance_then_canonical_order_and_caps_at_three(self):
        rows = [
            statement("normal-1", "Josh noted prior experience.", section="organizational_knowledge", position=1),
            statement("high-2", "Kendall recommended a partner.", section="organizational_knowledge", position=2, importance="high"),
            statement("high-3", "Tom raised a staffing question.", section="organizational_knowledge", position=3, importance="high"),
            statement("normal-4", "Ana shared research.", section="organizational_knowledge", position=4),
        ]
        result = build_guts_presentation(SimpleNamespace(statements=rows))
        self.assertEqual(
            [item.statement_key for item in result.internal_activity],
            ["high-2", "high-3", "normal-1"],
        )
        self.assertEqual(
            [item.text for item in result.internal_activity],
            ["Kendall recommended a partner.", "Tom raised a staffing question.", "Josh noted prior experience."],
        )

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
