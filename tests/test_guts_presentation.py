import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from bidlens.routes.api import generate_guts
from bidlens.services.opportunity_knowledge_brief import GUTSServiceError
from bidlens.services.opportunity_knowledge_brief.presentation import build_guts_presentation


def statement(key, text, *, placement="section", section=None, position=0, source_id=None):
    source = SimpleNamespace(
        source_id=source_id or f"source:{key}",
        citation_label=f"Citation {key}",
        source_class="current_state" if section == "current_state" else "official_evidence",
        source_type="test",
    )
    return SimpleNamespace(
        id=position + 1,
        statement_key=key,
        text=text,
        placement_type=placement,
        section_type=section,
        position=position,
        confidence="supported",
        importance="normal",
        source_links=[SimpleNamespace(brief_source=source)],
    )


class GUTSPresentationTests(unittest.TestCase):
    def test_maps_canonical_statements_without_rewriting_and_preserves_citations(self):
        rows = [
            statement("headline", "The opportunity remains active.", placement="headline"),
            statement("current-1", "The deadline is August 31.", section="current_state", position=1),
            statement("official-1", "Amendment 3 was released.", section="official_updates", position=2),
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
        self.assertEqual(result.risks_watch_items[0].text, "Staffing availability is unresolved.")
        self.assertEqual(result.internal_activity[0].statement_key, "org-1")
        self.assertEqual(result.internal_activity[0].citations[0].source_id, "source:org-1")

    def test_important_history_is_secondary_recent_context(self):
        history = statement("history-1", "The deadline changed previously.", section="important_history")
        result = build_guts_presentation(SimpleNamespace(statements=[history]))
        self.assertEqual(result.recent_developments, result.overall_status)
        self.assertEqual(result.recent_developments[0].statement_key, "history-1")

    def test_missing_generation_has_empty_sections_and_no_fabricated_recommendations(self):
        result = build_guts_presentation(None)
        self.assertEqual(result.overall_status, ())
        self.assertEqual(result.recent_developments, ())
        self.assertEqual(result.internal_activity, ())
        self.assertEqual(result.risks_watch_items, ())
        self.assertEqual(result.suggested_next_steps, ())


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
