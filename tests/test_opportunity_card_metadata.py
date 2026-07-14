import unittest
from pathlib import Path


class OpportunityCardMetadataTests(unittest.TestCase):
    def setUp(self):
        template = Path("src/bidlens/templates/_opp_card.html").read_text()
        self.collapsed, self.details = template.split("{# === EXPANDABLE DETAILS === #}", 1)

    def test_collapsed_card_uses_simplified_metadata_hierarchy(self):
        self.assertIn("opp-card-meta-line--collapsed", self.collapsed)
        self.assertIn("opp-card-agency", self.collapsed)
        self.assertIn("opp-card-metadata-row", self.collapsed)
        self.assertIn("primary_pursuit_lane.name", self.collapsed)
        self.assertNotIn("opportunity-type-pill", self.collapsed)
        self.assertNotIn("source-pill", self.collapsed)

    def test_secondary_metadata_remains_in_details(self):
        self.assertNotIn("opp.solicitation_number", self.collapsed)
        self.assertNotIn("opp.naics", self.collapsed)
        self.assertIn("opp.solicitation_number", self.details)
        self.assertIn("opp.naics", self.details)
        self.assertIn("opp.normalized_opportunity_type", self.details)


if __name__ == "__main__":
    unittest.main()
