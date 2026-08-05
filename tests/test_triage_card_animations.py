import unittest
from pathlib import Path


class TriageCardAnimationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path("src/bidlens/templates/base.html").read_text()
        self.triage = Path("src/bidlens/templates/triage.html").read_text()

    def test_qualify_reuses_interested_animation_path(self):
        self.assertIn(
            "beginOptimisticPursue(card, actionButton, true, { updateActionState: false })",
            self.triage,
        )
        self.assertIn("card.classList.add('opp-card--exit-forward');", self.base)
        self.assertIn("}, 180);", self.base)
        self.assertIn("}, 380);", self.base)

    def test_reject_reuses_archive_animation_path(self):
        self.assertIn(
            "beginOptimisticArchive(card, actionButton, true, { updateActionState: false })",
            self.triage,
        )
        self.assertIn("card.classList.add('opp-card--exit-away');", self.base)
        self.assertIn("}, 260);", self.base)

    def test_success_commits_and_failure_restores_card(self):
        failed_response = self.triage.index("if (!response.ok)")
        cancellation = self.triage.index("optimisticRemoval?.cancel();", failed_response)
        success_commit = self.triage.index("await optimisticRemoval.commit();")
        self.assertLess(cancellation, success_commit)
        self.assertIn("optimisticRemoval?.cancel();\n    showToast('Unable to update triage status'", self.triage)
        self.assertIn("card.hidden = false;", self.base)
        self.assertIn("if (shouldRemove && card.isConnected)", self.base)

    def test_existing_interested_and_archive_calls_keep_default_action_state(self):
        self.assertIn(
            "optimisticPursue = beginOptimisticPursue(\n            card,\n            actionButton,\n            optimisticShouldRemove\n          );",
            self.base,
        )
        self.assertIn(
            "optimisticArchive = beginOptimisticArchive(\n            card,\n            actionButton,\n            optimisticArchiveShouldRemove\n          );",
            self.base,
        )
        self.assertIn("{ updateActionState = true } = {}", self.base)


if __name__ == "__main__":
    unittest.main()
