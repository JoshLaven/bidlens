import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bidlens.services.account_aliases import (
    AccountAlias,
    AccountAliasConfigurationError,
    AccountAliasConflictError,
    build_account_alias_lookup,
    clear_account_alias_cache,
    get_account_alias_lookup,
    match_account_alias,
    normalize_account_lookup_key,
    resolve_account_display_name,
)


class AccountAliasNormalizationTests(unittest.TestCase):
    def tearDown(self):
        clear_account_alias_cache()

    def test_bundled_semantic_alias_file_loads(self):
        lookup = get_account_alias_lookup()

        self.assertGreater(len(lookup), 300)
        self.assertEqual(
            resolve_account_display_name("Administration for Children and Families - ACYF/CB"),
            "Administration for Children and Families",
        )

    def test_configured_alias_file_takes_precedence_and_is_cached(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text("Alias,Display\nU.S. Example Office,Approved Account\n", encoding="utf-8")
            with patch("bidlens.config.ACCOUNT_ALIAS_FILE_PATH", path):
                clear_account_alias_cache()
                first = get_account_alias_lookup()
                path.write_text("Alias,Display\nU.S. Example Office,Changed Account\n", encoding="utf-8")
                second = get_account_alias_lookup()

        self.assertIs(first, second)
        self.assertEqual(match_account_alias("US Example Office", second), "Approved Account")

    def test_configured_conflicting_aliases_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text(
                "Alias,Display\nResearch & Development,Account A\nResearch and Development,Account B\n",
                encoding="utf-8",
            )
            with patch("bidlens.config.ACCOUNT_ALIAS_FILE_PATH", path):
                clear_account_alias_cache()
                with self.assertRaises(AccountAliasConfigurationError):
                    get_account_alias_lookup()

    def test_configured_alias_file_requires_alias_and_display_columns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "aliases.csv"
            path.write_text("Account,Name\nExample,Example\n", encoding="utf-8")
            with patch("bidlens.config.ACCOUNT_ALIAS_FILE_PATH", path):
                clear_account_alias_cache()
                with self.assertRaises(AccountAliasConfigurationError):
                    get_account_alias_lookup()

    def test_missing_alias_uses_legacy_presentation_fallback(self):
        self.assertEqual(resolve_account_display_name("department.of.nih"), "Department Of NIH")

    def test_formatting_variants_share_one_lookup_key(self):
        expected = "united states department of health and human services"
        variants = (
            "US Department of Health & Human Services",
            "U.S. Department of Health and Human Services",
            "U.S Department  of Health, and Human Services",
            "United States Department of Health-and-Human Services",
            "  united states department of health and human services  ",
        )

        self.assertEqual({normalize_account_lookup_key(value) for value in variants}, {expected})

    def test_matching_normalizes_incoming_and_workbook_aliases(self):
        lookup = build_account_alias_lookup(
            [
                AccountAlias(
                    account="U.S. Department of Health & Human Services",
                    display_name="Department of Health and Human Services",
                )
            ]
        )

        self.assertEqual(
            match_account_alias(" us department  of health-and-human services ", lookup),
            "Department of Health and Human Services",
        )

    def test_workbook_column_names_are_supported(self):
        lookup = build_account_alias_lookup(
            [{"Alias": "Administration for Children & Families", "Display": "ACF"}]
        )

        self.assertEqual(match_account_alias("Administration for Children and Families", lookup), "ACF")

    def test_normalization_does_not_infer_semantic_aliases(self):
        pairs = (
            ("HHS", "Department of Health and Human Services"),
            ("Department of Health", "Department of Health and Human Services"),
            ("Office of Research", "Department of Research"),
            ("Veteran Affairs", "Veterans Affairs"),
            ("Dept of Education", "Department of Education"),
        )

        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertNotEqual(
                    normalize_account_lookup_key(left),
                    normalize_account_lookup_key(right),
                )

    def test_unmatched_account_is_not_semantically_inferred(self):
        lookup = build_account_alias_lookup(
            [AccountAlias(account="Department of Health and Human Services", display_name="HHS")]
        )

        self.assertIsNone(match_account_alias("HHS", lookup))

    def test_conflicting_formatting_equivalent_aliases_fail_closed(self):
        with self.assertRaises(AccountAliasConflictError):
            build_account_alias_lookup(
                [
                    AccountAlias(account="Research & Development", display_name="Account A"),
                    AccountAlias(account="Research and Development", display_name="Account B"),
                ]
            )


if __name__ == "__main__":
    unittest.main()
