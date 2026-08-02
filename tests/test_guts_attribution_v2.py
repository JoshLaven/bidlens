import datetime as dt
import json
import unittest

from pydantic import ValidationError

from bidlens.services.opportunity_knowledge_brief.contracts import (
    AttributionActor, CurrentOpportunityState, CurrentStateField, EvidenceAuthor,
    EvidenceSelection, EvidenceSelectionStatistics, EvidenceSource, GenerationConstraints,
    GUTSManifest, KnownConflict, ModelBriefingOutput, ModelOutputSection,
    ModelOutputStatement, SalesforceLinkState, StatementAttribution,
)
from bidlens.services.opportunity_knowledge_brief.output_schema import guts_output_schema
from bidlens.services.opportunity_knowledge_brief.model_client import _validation_feedback
from bidlens.services.opportunity_knowledge_brief.output_validation import (
    GUTSOutputValidator, GUTSValidationError,
)
from bidlens.services.opportunity_knowledge_brief.prompt import (
    GUTS_V8_SYSTEM_INSTRUCTIONS, GUTS_V9_SYSTEM_INSTRUCTIONS, manifest_input,
    resolve_prompt,
)


NOW = dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc)


def field(name, value=None):
    return CurrentStateField(value=value, source_id=f"current_state:opportunity:1:{name}")


def source(source_id, source_type, text, author=None):
    return EvidenceSource(
        source_id=source_id, source_class="organizational_knowledge",
        source_type=source_type, authority="attributed_claim", citation_label=source_id,
        text=text, author=author, selected_character_count=len(text),
        original_character_count=len(text),
        provenance={"organization_id": 1, "workspace_id": 1, "opportunity_id": 1},
    )


def manifest(*, conflict=False):
    state = CurrentOpportunityState(
        opportunity_id=1, organization_id=1, workspace_id=1,
        title=field("title", "Synthetic Evaluation"), client=field("client", "Example Agency"),
        description=field("description", "Synthetic description"),
        response_deadline=field("response_deadline", "2026-09-01"),
        posted_date=field("posted_date", "2026-07-01"),
        solicitation_number=field("solicitation_number", "SYN-1"),
        opportunity_type=field("opportunity_type", "RFP"), source_stage=field("source_stage", "active"),
        source=field("source", "manual"), source_record_id=field("source_record_id", "syn-1"),
        source_url=field("source_url"), sam_url=field("sam_url"), bidlens_id=field("bidlens_id", "syn"),
        sam_notice_id=field("sam_notice_id"), naics=field("naics"), naics_title=field("naics_title"),
        set_aside=field("set_aside"), description_original_character_count=21,
        description_was_truncated=False, salesforce=SalesforceLinkState(linked=False),
    )
    sources = (
        source("note:alex", "note", "Alex plans to contact Westat.", EvidenceAuthor(
            user_id=2, display_name="Alex Rivera", address="alex@example.test",
        )),
        source("email:pat", "email", "Pat raised a staffing concern.", EvidenceAuthor(
            display_name="Pat Lee", address="PAT@EXAMPLE.TEST",
        )),
        source("email:morgan", "email", "Morgan recommended early outreach.", EvidenceAuthor(
            user_id=3, display_name="Morgan Diaz", address="morgan@example.test",
        )),
        source("note:authorless", "note", "An internal record identifies relevant experience."),
    )
    stats = EvidenceSelectionStatistics(
        available_source_count=4, unavailable_source_count=0,
        selected_character_count=sum(len(item.text) for item in sources),
        original_character_count=sum(len(item.text) for item in sources), truncated_source_count=0,
    )
    conflicts = (KnownConflict(
        conflict_id="synthetic-conflict", field_name="partner",
        authoritative_value="unresolved", authoritative_source_id="note:alex",
        conflicting_value="proposed", conflicting_source_id="email:morgan",
        resolution="authoritative_current_wins",
    ),) if conflict else ()
    return GUTSManifest(
        manifest_version="guts-manifest-v1", opportunity_id=1, organization_id=1, workspace_id=1,
        snapshot_started_at=NOW, snapshot_completed_at=NOW, current_state=state,
        evidence=EvidenceSelection(sources=sources, known_conflicts=conflicts, statistics=stats),
        constraints=GenerationConstraints(
            max_total_input_characters=100000, max_output_tokens=2400,
            timeout_seconds=45.0, max_retries=1,
        ), reproducibility_status="fully_reproducible",
    )


def actor(user_id, name, email):
    return AttributionActor(user_id=user_id, display_name=name, email=email)


def attribution(*actors):
    return StatementAttribution(type="person", actors=actors)


def output(statement):
    headline = ModelOutputStatement(
        statement_key="headline", text="The opportunity remains active.", importance="high",
        confidence="supported", source_ids=(field("source_stage").source_id,), attribution=None,
    )
    summary = ModelOutputStatement(
        statement_key="summary-1", text="The opportunity remains active.", importance="normal",
        confidence="supported", source_ids=(field("source_stage").source_id,), attribution=None,
    )
    return ModelBriefingOutput(
        headline=headline, summary_statements=(summary,),
        sections=(ModelOutputSection(
            section_type="organizational_knowledge", statements=(statement,),
        ),),
    )


def organizational(text, source_ids, structured):
    return ModelOutputStatement(
        statement_key="org-1", text=text, importance="normal", confidence="attributed",
        source_ids=tuple(source_ids), attribution=structured,
    )


class AttributionContractTests(unittest.TestCase):
    def test_valid_internal_external_multiple_and_internal_source(self):
        internal = actor(2, "  Alex   Rivera ", "ALEX@EXAMPLE.TEST")
        external = actor(None, "Pat Lee", "pat@example.test")
        combined = attribution(internal, external)
        self.assertEqual(internal.display_name, "Alex Rivera")
        self.assertEqual(internal.email, "alex@example.test")
        self.assertEqual([item.user_id for item in combined.actors], [2, None])
        self.assertEqual(StatementAttribution(type="internal_source", actors=()).actors, ())

    def test_malformed_unknown_and_duplicate_actors_fail(self):
        cases = (
            {"type": "person", "actors": []},
            {"type": "internal_source", "actors": [{"user_id": None, "display_name": "Pat", "email": None}]},
            {"type": "person", "actors": [{"user_id": 2, "display_name": "Alex", "email": None}]},
            {"type": "person", "actors": [{"user_id": None, "display_name": None, "email": None}]},
            {"type": "person", "actors": [
                {"user_id": None, "display_name": "Pat", "email": "pat@example.test"},
                {"user_id": None, "display_name": "Other", "email": "PAT@example.test"},
            ]},
            {"type": "person", "actors": [{"user_id": None, "display_name": "Pat", "email": None, "extra": 1}]},
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                StatementAttribution.model_validate(value)


class AttributionSchemaAndPromptTests(unittest.TestCase):
    def test_registry_keeps_v8_and_selects_real_v9(self):
        self.assertEqual(resolve_prompt("guts-v8").instructions, GUTS_V8_SYSTEM_INSTRUCTIONS)
        v9 = resolve_prompt("guts-v9")
        self.assertEqual(v9.output_schema_version, "guts-output-v2")
        self.assertEqual(v9.instructions, GUTS_V9_SYSTEM_INSTRUCTIONS)
        self.assertIn("structured attribution", v9.instructions)
        self.assertIn("source_attribution attached to the eligible cited", v9.instructions)
        self.assertIn("Do not substitute another person with the same display name", v9.instructions)
        self.assertTrue(v9.instructions.startswith(GUTS_V8_SYSTEM_INSTRUCTIONS))

    def test_runtime_input_attaches_exact_attribution_to_each_source(self):
        base = manifest()
        same_name = source(
            "email:other-pat", "email", "A different Pat shared other context.",
            EvidenceAuthor(display_name="Pat Lee", address="other-pat@example.test"),
        )
        expanded = base.model_copy(update={
            "evidence": base.evidence.model_copy(update={
                "sources": (*base.evidence.sources, same_name),
            }),
        })

        payload = json.loads(manifest_input(expanded, prompt=resolve_prompt("guts-v9")))
        contract = payload["attribution_contract"]["source_attributions"]
        self.assertEqual(contract["email:pat"], {
            "type": "person", "actors": [{
                "user_id": None, "display_name": "Pat Lee", "email": "pat@example.test",
            }],
        })
        self.assertEqual(contract["email:other-pat"], {
            "type": "person", "actors": [{
                "user_id": None, "display_name": "Pat Lee",
                "email": "other-pat@example.test",
            }],
        })
        self.assertEqual(contract["note:alex"]["actors"][0]["user_id"], 2)
        self.assertEqual(contract["note:authorless"], {
            "type": "internal_source", "actors": [],
        })
        source_rows = {
            item["source_id"]: item
            for item in payload["evidence"]["evidence"]["sources"]
        }
        self.assertEqual(source_rows["email:pat"]["source_attribution"], contract["email:pat"])
        self.assertEqual(
            source_rows["email:other-pat"]["source_attribution"],
            contract["email:other-pat"],
        )
        self.assertNotEqual(
            source_rows["email:pat"]["source_attribution"],
            source_rows["email:other-pat"]["source_attribution"],
        )

    def test_v2_schema_requires_strict_attribution_and_retains_existing_constraints(self):
        schema = guts_output_schema(
            allowed_source_ids=("note:alex",), output_schema_version="guts-output-v2",
            allowed_actors=[{
                "user_id": 2, "display_name": "Alex Rivera", "email": "alex@example.test",
            }],
        )
        statement = schema["properties"]["summary_statements"]["items"]
        self.assertIn("attribution", statement["required"])
        self.assertEqual(statement["properties"]["source_ids"]["items"]["enum"], ["note:alex"])
        self.assertNotIn("statement_key", schema["properties"]["headline"]["properties"])
        variants = statement["properties"]["attribution"]["anyOf"]
        self.assertEqual(variants[0], {"type": "null"})
        self.assertFalse(variants[1]["additionalProperties"])
        self.assertEqual(variants[2]["properties"]["actors"]["maxItems"], 0)
        actor_schema = variants[1]["properties"]["actors"]["items"]["anyOf"][0]
        self.assertEqual(actor_schema["required"], ["user_id", "display_name", "email"])
        self.assertEqual(actor_schema["properties"]["user_id"]["enum"], [2])
        self.assertEqual(actor_schema["properties"]["display_name"]["enum"], ["Alex Rivera"])
        self.assertFalse(actor_schema["additionalProperties"])

    def test_v1_schema_does_not_add_attribution(self):
        schema = guts_output_schema(output_schema_version="guts-output-v1")
        statement = schema["properties"]["summary_statements"]["items"]
        self.assertNotIn("attribution", statement["properties"])


class AttributionValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = GUTSOutputValidator(output_schema_version="guts-output-v2")

    def validate(self, statement, *, conflict=False):
        return self.validator.validate(output(statement), manifest(conflict=conflict))

    def test_internal_note_external_email_and_multiple_actor_correspondence(self):
        cases = (
            organizational("Alex Rivera plans to contact Westat.", ["note:alex"], attribution(
                actor(2, "Alex Rivera", "alex@example.test"),
            )),
            organizational("Pat Lee raised a staffing concern.", ["email:pat"], attribution(
                actor(None, "Pat Lee", "PAT@EXAMPLE.TEST"),
            )),
            organizational(
                "Alex Rivera and Morgan Diaz both recommended early outreach.",
                ["note:alex", "email:morgan"], attribution(
                    actor(2, "Alex Rivera", "alex@example.test"),
                    actor(3, "Morgan Diaz", "morgan@example.test"),
                ),
            ),
        )
        for statement in cases:
            with self.subTest(text=statement.text):
                validated = self.validate(statement)
                saved = validated.briefing.sections[0].statements[0]
                self.assertEqual(saved.attribution, statement.attribution)

    def test_missing_unmatched_and_one_unmatched_actor_fail(self):
        cases = (
            (organizational("Alex Rivera plans outreach.", ["note:alex"], None), "attribution_missing"),
            (organizational("Jamie plans outreach.", ["note:alex"], attribution(
                actor(None, "Jamie", "jamie@example.test"),
            )), "actor_source_mismatch"),
            (organizational("Wrong Name plans outreach.", ["email:pat"], attribution(
                actor(None, "Wrong Name", "pat@example.test"),
            )), "actor_source_mismatch"),
            (organizational("Alex Rivera and Jamie recommended outreach.", ["note:alex"], attribution(
                actor(2, "Alex Rivera", "alex@example.test"),
                actor(None, "Jamie", "jamie@example.test"),
            )), "actor_source_mismatch"),
        )
        for statement, rule in cases:
            with self.subTest(rule=rule), self.assertRaises(GUTSValidationError) as raised:
                self.validate(statement)
            self.assertEqual(raised.exception.validator_rule, rule)

    def test_display_name_only_match_must_be_source_local_and_unambiguous(self):
        base = manifest()
        duplicate_name = source(
            "email:other-pat", "email", "Another Pat shared separate context.",
            EvidenceAuthor(display_name="Pat Lee", address="other-pat@example.test"),
        )
        expanded = base.model_copy(update={
            "evidence": base.evidence.model_copy(update={
                "sources": (*base.evidence.sources, duplicate_name),
            }),
        })
        statement = organizational(
            "Pat Lee raised a staffing concern.", ["email:pat", "email:other-pat"],
            attribution(actor(None, "Pat Lee", None)),
        )
        with self.assertRaises(GUTSValidationError) as raised:
            self.validator.validate(output(statement), expanded)
        self.assertEqual(raised.exception.validator_rule, "ambiguous_actor_identity")

    def test_same_display_name_must_copy_actor_from_the_cited_source(self):
        base = manifest()
        other_pat = source(
            "email:other-pat", "email", "A different Pat recommended another approach.",
            EvidenceAuthor(display_name="Pat Lee", address="other-pat@example.test"),
        )
        expanded = base.model_copy(update={
            "evidence": base.evidence.model_copy(update={
                "sources": (*base.evidence.sources, other_pat),
            }),
        })
        exact = organizational(
            "Pat Lee raised a staffing concern.", ["email:pat"],
            attribution(actor(None, "Pat Lee", "pat@example.test")),
        )
        self.validator.validate(output(exact), expanded)

        wrong_source_actor = exact.model_copy(update={
            "attribution": attribution(actor(
                None, "Pat Lee", "other-pat@example.test",
            )),
        })
        with self.assertRaises(GUTSValidationError) as raised:
            self.validator.validate(output(wrong_source_actor), expanded)
        self.assertEqual(raised.exception.validator_rule, "actor_source_mismatch")
        feedback = _validation_feedback(raised.exception)
        self.assertIn("Copy attribution only from source_attribution", feedback)
        self.assertIn("cited source IDs", feedback)
        self.assertIn("same display name", feedback)
        self.assertNotIn("pat@example.test", feedback)
        self.assertNotIn("other-pat@example.test", feedback)

    def test_multiple_cited_sources_support_each_distinct_same_name_actor(self):
        base = manifest()
        other_pat = source(
            "email:other-pat", "email", "A different Pat shared complementary context.",
            EvidenceAuthor(display_name="Pat Lee", address="other-pat@example.test"),
        )
        expanded = base.model_copy(update={
            "evidence": base.evidence.model_copy(update={
                "sources": (*base.evidence.sources, other_pat),
            }),
        })
        statement = organizational(
            "Pat Lee and another Pat Lee shared complementary context.",
            ["email:pat", "email:other-pat"],
            attribution(
                actor(None, "Pat Lee", "pat@example.test"),
                actor(None, "Pat Lee", "other-pat@example.test"),
            ),
        )
        validated = self.validator.validate(output(statement), expanded)
        self.assertEqual(
            validated.briefing.sections[0].statements[0].attribution,
            statement.attribution,
        )

    def test_internal_source_authorless_passes_but_resolvable_source_fails(self):
        internal = StatementAttribution(type="internal_source", actors=())
        self.validate(organizational(
            "An internal note indicates relevant experience.", ["note:authorless"], internal,
        ))
        with self.assertRaises(GUTSValidationError) as raised:
            self.validate(organizational(
                "An internal note indicates relevant experience.", ["note:alex"], internal,
            ))
        self.assertEqual(raised.exception.validator_rule, "unsupported_internal_source")

    def test_team_scope_and_conflicting_consensus_remain_strict(self):
        single = organizational("The team recommended outreach.", ["note:alex"], attribution(
            actor(2, "Alex Rivera", "alex@example.test"),
        ))
        with self.assertRaises(GUTSValidationError):
            self.validate(single)
        combined = organizational(
            "Alex Rivera and Morgan Diaz both recommended early outreach.",
            ["note:alex", "email:morgan"], attribution(
                actor(2, "Alex Rivera", "alex@example.test"),
                actor(3, "Morgan Diaz", "morgan@example.test"),
            ),
        )
        self.validate(combined)
        with self.assertRaises(GUTSValidationError) as raised:
            self.validate(combined, conflict=True)
        self.assertEqual(raised.exception.validator_rule, "consensus_scope_invalid")

    def test_epistemic_status_and_non_attributed_null_remain_strict(self):
        self.validate(organizational("Alex Rivera plans to contact Westat.", ["note:alex"], attribution(
            actor(2, "Alex Rivera", "alex@example.test"),
        )))
        self.validate(organizational("Pat Lee raised a staffing concern.", ["email:pat"], attribution(
            actor(None, "Pat Lee", "pat@example.test"),
        )))
        proposed = organizational(
            "Alex Rivera proposed Westat as a potential subcontractor.", ["note:alex"],
            attribution(actor(2, "Alex Rivera", "alex@example.test")),
        )
        self.validate(proposed)
        upgraded = proposed.model_copy(update={"text": "Westat is the subcontractor."})
        with self.assertRaises(GUTSValidationError):
            self.validate(upgraded)

        official_with_actor = output(proposed).headline.model_copy(update={
            "attribution": attribution(actor(2, "Alex Rivera", "alex@example.test")),
        })
        with self.assertRaises(GUTSValidationError) as raised:
            self.validator.validate(ModelBriefingOutput(
                headline=official_with_actor,
                summary_statements=output(proposed).summary_statements,
                sections=output(proposed).sections,
            ), manifest())
        self.assertEqual(raised.exception.validator_rule, "attribution_unexpected")

    def test_organizational_concern_cannot_be_anonymous_uncertain_fact(self):
        concern = organizational(
            "Pat Lee raised a staffing concern.", ["email:pat"],
            attribution(actor(None, "Pat Lee", "pat@example.test")),
        )
        self.validate(concern)
        anonymous = concern.model_copy(update={
            "text": "Staffing remains uncertain.", "confidence": "uncertain",
            "attribution": None,
        })
        with self.assertRaises(GUTSValidationError) as raised:
            self.validate(anonymous)
        self.assertEqual(raised.exception.safe_message, "An organizational uncertainty lacked attribution.")


if __name__ == "__main__":
    unittest.main()
