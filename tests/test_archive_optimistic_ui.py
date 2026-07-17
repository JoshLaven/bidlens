import unittest
from pathlib import Path


class ArchiveOptimisticUiTests(unittest.TestCase):
    def setUp(self):
        self.base_template = Path("src/bidlens/templates/base.html").read_text()
        self.card_template = Path("src/bidlens/templates/_opp_card.html").read_text()

    def test_archive_buttons_expose_action_hook_used_by_vote_handler(self):
        pass_button_fragment = (
            'data-vote-button="PASS"\n'
            '                data-action-opp-id="{{ opp.id }}"'
        )
        self.assertIn(pass_button_fragment, self.card_template)

    def test_archive_uses_optimistic_state_and_duplicate_click_guard(self):
        self.assertIn("function beginOptimisticArchive", self.base_template)
        self.assertIn("card.dataset.votePending = 'true';", self.base_template)
        self.assertIn("actionButton?.disabled || card?.dataset.votePending === 'true'", self.base_template)
        self.assertIn("setPassButtonState(button, true);", self.base_template)
        self.assertIn("card.classList.add('opp-card--exit-away');", self.base_template)
        self.assertIn("card.hidden = true;", self.base_template)

    def test_archive_success_commits_removal_and_updates_count(self):
        self.assertIn("await optimisticArchive.commit();", self.base_template)
        self.assertIn("card.remove();", self.base_template)
        self.assertIn("decrementQueueResultCount();", self.base_template)
        self.assertIn("updateOpportunityEmptyState();", self.base_template)

    def test_archive_failure_rolls_back_card_and_action(self):
        self.assertIn("optimisticArchive?.cancel();", self.base_template)
        self.assertIn("card.hidden = false;", self.base_template)
        self.assertIn("button.className = original.buttonClassName;", self.base_template)
        self.assertIn("button.innerHTML = original.buttonHtml;", self.base_template)
        self.assertIn("showToast(error?.message || 'Action failed', 'error');", self.base_template)


if __name__ == "__main__":
    unittest.main()
