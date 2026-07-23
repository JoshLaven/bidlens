import unittest

from bidlens.services.agency_display import agency_display, agency_presentation


class AgencyDisplayTests(unittest.TestCase):
    def test_dotted_source_path_has_consistent_display_parent_and_subagency(self):
        agency = agency_presentation(
            "HEALTH_AND_HUMAN_SERVICES.DEPARTMENT.CENTERS_FOR_MEDICARE_AND_MEDICAID_SERVICES"
        )

        self.assertEqual(agency.display, "Centers For Medicare And Medicaid Services")
        self.assertEqual(agency.parent, "Health And Human Services")
        self.assertEqual(agency.sub_agency, "Centers For Medicare And Medicaid Services")

    def test_single_agency_uses_same_display_and_parent_without_subagency(self):
        agency = agency_presentation("Department of Health and Human Services")

        self.assertEqual(agency_display("Department of Health and Human Services"), agency.display)
        self.assertEqual(agency.parent, "Department Of Health And Human Services")
        self.assertIsNone(agency.sub_agency)

    def test_common_government_acronyms_are_preserved(self):
        examples = {
            "hhs.cms": ("HHS CMS", "HHS", "CMS"),
            "nih": ("NIH", "NIH", None),
            "samhsa": ("SAMHSA", "SAMHSA", None),
            "department.of.va": ("Department Of VA", "Department", "VA"),
            "nasa.office": ("NASA", "NASA", "Office"),
            "usda.epa.gsa": ("USDA EPA GSA", "USDA", "GSA"),
            "dod.dhs.doe.faa.dot": ("DOD DHS DOE FAA DOT", "DOD", "DOT"),
        }

        for raw_agency, expected in examples.items():
            with self.subTest(raw_agency=raw_agency):
                agency = agency_presentation(raw_agency)
                self.assertEqual(
                    (agency.display, agency.parent, agency.sub_agency),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
