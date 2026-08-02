"""Development and administrative command-line tools for BidLens."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, TextIO

from sqlalchemy import inspect, text

from . import config
from .database import SessionLocal
from .models import OpportunityKnowledgeBriefGeneration, OrganizationMembership, User
from .services.opportunity_knowledge_brief import (
    GUTSServiceError, OpportunityKnowledgeBriefService,
)
from .services.opportunity_knowledge_brief.model_client import (
    GUTSModelError, probe_guts_model,
)


SECTION_ORDER = (
    "current_state", "official_updates", "organizational_knowledge",
    "important_history", "uncertainties",
)
PLACEMENT_ORDER = {"headline": 0, "summary": 1, "section": 2}
GUTS_GENERATION_TABLE = OpportunityKnowledgeBriefGeneration.__tablename__
_BIDLENS_IDENTITY_TABLES = frozenset({"alembic_version", "opportunities", "users"})


def _line(output: TextIO, value: str = "") -> None:
    print(value, file=output)


def _database_identity(bind) -> dict[str, object]:
    """Return a credential-free identity for the session's actual database bind."""
    url = getattr(bind, "url", None)
    if url is None:
        return {"backend": "other", "host": None, "port": None, "database": None}
    backend = str(url.get_backend_name() or "other").lower()
    if backend == "sqlite":
        database = url.database or ":memory:"
        if database != ":memory:":
            database = Path(database).name
        return {"backend": "sqlite", "host": None, "port": None, "database": database}
    if backend == "postgresql":
        return {
            "backend": "postgresql", "host": url.host,
            "port": url.port or 5432, "database": url.database,
        }
    return {
        "backend": "other", "host": url.host,
        "port": url.port, "database": url.database,
    }


def _safe_scalar(value: object, *, limit: int = 80) -> str:
    rendered = str(value or "unknown").replace("\n", " ").replace("\r", " ")
    return rendered[:limit]


def _database_preflight(
    *, session_factory: Callable = SessionLocal, output: TextIO,
) -> int:
    """Print safe, read-only database identity and GUTS lifecycle metadata."""
    db = None
    try:
        db = session_factory()
        bind = db.get_bind()
        identity = _database_identity(bind)
        _line(output, "BidLens database preflight (read-only)")
        _line(output, f"backend={identity['backend']}")
        _line(output, f"host={identity['host'] or 'local'}")
        _line(output, f"port={identity['port'] or 'default'}")
        _line(output, f"database={identity['database'] or 'unknown'}")

        if identity["backend"] not in {"sqlite", "postgresql"} or not identity["database"]:
            _line(output, "warning=unsafe or unrecognized database configuration")
            return 1
        if identity["backend"] == "sqlite":
            _line(output, "warning=SQLite is a local development database, not Railway production PostgreSQL")

        connection = db.connection()
        if identity["backend"] == "postgresql":
            connection.execute(text("SET TRANSACTION READ ONLY"))
        table_names = set(inspect(connection).get_table_names())
        identifiable = bool(table_names & _BIDLENS_IDENTITY_TABLES) or GUTS_GENERATION_TABLE in table_names
        if not identifiable:
            _line(output, "guts_generation_table=false")
            _line(output, "alembic_revision=unavailable")
            _line(output, "warning=database does not appear to contain a BidLens schema")
            return 1

        revision = "unavailable"
        if "alembic_version" in table_names:
            revision = _safe_scalar(connection.execute(text(
                "SELECT version_num FROM alembic_version LIMIT 1"
            )).scalar_one_or_none())
        _line(output, f"alembic_revision={revision}")

        if GUTS_GENERATION_TABLE not in table_names:
            _line(output, "guts_generation_table=false")
            _line(output, "maximum_generation_id=unavailable")
            _line(output, "warning=GUTS generation table does not exist")
            return 0

        _line(output, "guts_generation_table=true")
        maximum_id = connection.execute(text(
            f"SELECT MAX(id) FROM {GUTS_GENERATION_TABLE}"
        )).scalar_one_or_none()
        _line(output, f"maximum_generation_id={maximum_id if maximum_id is not None else 'none'}")
        rows = connection.execute(text(
            f"SELECT id, opportunity_id, organization_id, status, created_at "
            f"FROM {GUTS_GENERATION_TABLE} ORDER BY id DESC LIMIT 5"
        )).mappings().all()
        _line(output, "recent_generations:")
        if not rows:
            _line(output, "  none")
        for row in rows:
            created_at = row["created_at"]
            created = created_at.isoformat() if hasattr(created_at, "isoformat") else _safe_scalar(created_at)
            _line(
                output,
                "  " + " ".join((
                    f"id={row['id']}", f"opportunity_id={row['opportunity_id']}",
                    f"organization_id={row['organization_id']}",
                    f"status={_safe_scalar(row['status'])}", f"created_at={created}",
                )),
            )
        return 0
    except Exception:
        _line(output, "BidLens database preflight (read-only)")
        _line(output, "warning=database configuration is missing or the database could not be reached")
        return 1
    finally:
        if db is not None:
            db.close()


def _failure(
    output: TextIO, *, category: str, message: str, stage: str,
    generation_id: int | None = None,
) -> int:
    _line(output, "GUTS generation failed")
    _line(output, f"Category: {category}")
    _line(output, f"Message: {message}")
    _line(output, f"Stage: {stage}")
    if generation_id is not None:
        _line(output, f"Generation ID: {generation_id}")
    return 1


def _render_validation_debug(output: TextIO, details: dict) -> None:
    """Render one rejected statement to this CLI stream only; never log or persist it."""
    _line(output, "Validation debug (development CLI only):")
    _line(output, f"  Rule: {details.get('validator_rule', 'unknown')}")
    _line(output, f"  Reason: {details.get('validator_reason', 'Unavailable')}")
    _line(output, f"  Statement key: {details.get('statement_key', 'unknown')}")
    _line(output, f"  Placement: {details.get('placement', 'unknown')}")
    _line(output, f"  Section type: {details.get('section_type') or 'None'}")
    _line(output, f"  Confidence: {details.get('confidence', 'unknown')}")
    if details.get("grounded_field"):
        _line(output, f"  Grounded field: {details['grounded_field']}")
    source_ids = details.get("cited_source_ids") or ()
    source_kinds = details.get("cited_source_kinds") or ()
    allowed_classes = details.get("allowed_source_classes") or ()
    required_classes = details.get("required_source_classes") or ()
    _line(output, f"  Cited source IDs: {', '.join(source_ids) if source_ids else 'None'}")
    _line(output, f"  Cited source classes/types: {', '.join(source_kinds) if source_kinds else 'None'}")
    if allowed_classes:
        _line(output, f"  Allowed source classes: {', '.join(allowed_classes)}")
    if required_classes:
        _line(output, f"  Required source classes: {', '.join(required_classes)}")
    if details.get("required_source_id"):
        _line(output, f"  Required source ID: {details['required_source_id']}")
    required_ids = details.get("required_source_ids") or ()
    other_required_ids = tuple(
        source_id for source_id in required_ids if source_id != details.get("required_source_id")
    )
    if other_required_ids:
        _line(output, f"  Other required source IDs: {', '.join(other_required_ids)}")
    _line(output, f"  Rejected statement: {details.get('statement_text', 'Unavailable')}")


def _render_provider_debug(output: TextIO, details: dict) -> None:
    """Render sanitized provider metadata to this CLI stream only."""
    _line(output, "Provider debug (development CLI only):")
    fields = (
        ("Provider", "provider"), ("Configured model", "model"), ("Subtype", "subtype"),
        ("HTTP status", "http_status"), ("Provider code", "provider_code"),
        ("Provider type", "provider_type"), ("Parameter", "parameter"),
        ("Request ID", "request_id"), ("Retryable", "retryable"),
        ("Safe explanation", "safe_explanation"),
    )
    for label, key in fields:
        value = details.get(key)
        if value is not None:
            _line(output, f"  {label}: {str(value).lower() if isinstance(value, bool) else value}")


def _render_schema_debug(output: TextIO, details: dict) -> None:
    """Render one sanitized structured-output error to this CLI stream only."""
    _line(output, "Schema debug (development CLI only):")
    fields = (
        ("Diagnostic rule", "diagnostic_rule"), ("Parse stage", "parse_stage"),
        ("Error class", "error_class"), ("Schema error type", "schema_error_type"),
        ("Path", "path"), ("Expected", "expected"),
        ("Received type", "received_type"), ("Missing field", "missing_field"),
        ("Unexpected field", "unexpected_field"),
        ("Invalid enum value", "invalid_enum_value"), ("Attempt", "attempt"),
        ("Received key", "received_key"), ("Required key", "required_key"),
        ("Earlier attempt also failed", "earlier_attempt_failed"),
        ("Safe reason", "safe_reason"),
    )
    for label, key in fields:
        value = details.get(key)
        if value is not None:
            _line(output, f"  {label}: {str(value).lower() if isinstance(value, bool) else value}")


def _resolve_membership(db, *, user: User, organization_id: int | None):
    query = db.query(OrganizationMembership).filter(OrganizationMembership.user_id == user.id)
    if organization_id is not None:
        membership = query.filter(OrganizationMembership.organization_id == organization_id).one_or_none()
        if membership is None:
            raise ValueError("The specified user is not a member of that organization.")
        return membership
    memberships = query.order_by(OrganizationMembership.organization_id.asc()).all()
    if len(memberships) != 1:
        raise ValueError("The user's active organization is ambiguous; provide --organization-id.")
    return memberships[0]


def _citation_labels(statement) -> str:
    labels = [link.brief_source.citation_label for link in statement.source_links]
    return "; ".join(labels) if labels else "None"


def render_generation(generation, *, output: TextIO) -> None:
    """Render persisted briefing content and safe provenance labels only."""
    _line(output, "GUTS generation completed")
    _line(output, f"Generation ID: {generation.id}")
    _line(output, f"Status: {generation.status}")
    _line(output, f"Opportunity ID: {generation.opportunity_id}")
    _line(output, f"Manifest hash: {generation.manifest_hash or 'Unavailable'}")
    _line(output, f"Provider/model: {generation.provider or 'Unavailable'} / {generation.model or 'Unavailable'}")
    _line(output, f"Total duration: {generation.total_ms if generation.total_ms is not None else 'Unavailable'} ms")

    _line(output, "Source counts:")
    source_summary = generation.source_summary_json if isinstance(generation.source_summary_json, dict) else {}
    if source_summary:
        for name, counts in source_summary.items():
            if isinstance(counts, dict):
                rendered = ", ".join(f"{key}={value}" for key, value in counts.items())
                _line(output, f"  {name}: {rendered}")
    else:
        _line(output, f"  persisted={len(generation.sources)}")

    _line(output, "Warnings:")
    warnings = generation.warning_metadata_json if isinstance(generation.warning_metadata_json, list) else []
    if warnings:
        for warning in warnings:
            if isinstance(warning, dict):
                _line(output, f"  - {warning.get('type', 'warning')}: {warning.get('text', '')}")
    else:
        _line(output, "  None")

    statements = sorted(
        generation.statements,
        key=lambda item: (
            PLACEMENT_ORDER.get(item.placement_type, 9),
            SECTION_ORDER.index(item.section_type) if item.section_type in SECTION_ORDER else 9,
            item.position, item.id,
        ),
    )
    headline = next((item for item in statements if item.placement_type == "headline"), None)
    _line(output, "Headline:")
    if headline:
        _line(output, f"  {headline.text}")
        _line(output, f"  Citations: {_citation_labels(headline)}")
    else:
        _line(output, "  Unavailable")

    _line(output, "Summary:")
    summary = [item for item in statements if item.placement_type == "summary"]
    if summary:
        for item in summary:
            _line(output, f"  - {item.text}")
            _line(output, f"    Citations: {_citation_labels(item)}")
    else:
        _line(output, "  None")

    sections = [item for item in statements if item.placement_type == "section"]
    if sections:
        for section_type in SECTION_ORDER:
            section_statements = [item for item in sections if item.section_type == section_type]
            if not section_statements:
                continue
            title = section_statements[0].section_title or section_type.replace("_", " ").title()
            _line(output, f"{title}:")
            for item in section_statements:
                _line(output, f"  - {item.text}")
                _line(output, f"    Citations: {_citation_labels(item)}")


def _generate_guts(
    args, *, session_factory: Callable = SessionLocal,
    service_factory: Callable = OpportunityKnowledgeBriefService,
    output: TextIO,
) -> int:
    if not config.GUTS_ENABLED:
        return _failure(
            output, category="access_denied", message="Get Up to Speed is not enabled.",
            stage="configuration",
        )
    db = session_factory()
    try:
        user = db.get(User, args.user_id)
        if user is None:
            return _failure(
                output, category="user_not_found", message="The specified user was not found.",
                stage="authorization",
            )
        try:
            membership = _resolve_membership(
                db, user=user, organization_id=args.organization_id,
            )
        except ValueError as exc:
            return _failure(output, category="access_denied", message=str(exc), stage="authorization")
        # Match the request-established transient tenancy context used by the web app.
        user.current_organization_id = membership.organization_id
        user.current_role = membership.role
        try:
            generation = service_factory(db).generate(
                opportunity_id=args.opportunity_id, requesting_user=user,
                active_organization_id=membership.organization_id,
            )
        except GUTSServiceError as exc:
            status = _failure(
                output, category=exc.safe_category, message=exc.safe_message,
                stage=exc.stage, generation_id=exc.generation_id,
            )
            if args.debug_validation and exc.validation_debug:
                _render_validation_debug(output, exc.validation_debug)
            if args.debug_provider and exc.provider_debug:
                _render_provider_debug(output, exc.provider_debug)
            if args.debug_schema and exc.schema_debug:
                _render_schema_debug(output, exc.schema_debug)
            return status
        render_generation(generation, output=output)
        return 0
    except Exception:
        db.rollback()
        return _failure(
            output, category="unexpected_error",
            message="The development GUTS command could not complete.", stage="command",
        )
    finally:
        db.close()


def _probe_guts(args, *, output: TextIO, probe_factory: Callable = probe_guts_model) -> int:
    try:
        result = probe_factory(model=args.model)
    except GUTSModelError as exc:
        return _failure(output, category=exc.safe_category, message=exc.safe_message, stage=exc.stage)
    _line(output, "GUTS model probe (development CLI only)")
    _line(output, f"Success: {str(result.success).lower()}")
    _line(output, f"Provider: {result.provider}")
    _line(output, f"Resolved model: {result.model}")
    if result.diagnostic:
        _render_provider_debug(output, result.diagnostic.as_dict())
    return 0 if result.success else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bidlens.cli",
        description="BidLens development and administrative commands. Not a user-facing interface.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser(
        "generate-guts",
        help="Development-only: generate and safely render a persisted GUTS briefing.",
    )
    generate.add_argument("--opportunity-id", type=int, required=True)
    generate.add_argument("--user-id", type=int, required=True)
    generate.add_argument("--organization-id", type=int)
    generate.add_argument(
        "--debug-validation", action="store_true",
        help="Development CLI only: print the single rejected validation statement to this terminal.",
    )
    generate.add_argument(
        "--debug-provider", action="store_true",
        help="Development CLI only: print sanitized provider failure metadata to this terminal.",
    )
    generate.add_argument(
        "--debug-schema", action="store_true",
        help="Development CLI only: print sanitized final structured-output schema metadata.",
    )
    probe = commands.add_parser(
        "probe-guts-model",
        help="Development-only: make one evidence-free structured model compatibility request.",
    )
    probe.add_argument("--model", help="Temporary model override for this probe only.")
    commands.add_parser(
        "database-preflight",
        help="Read-only: identify the resolved database before GUTS production debugging.",
    )
    return parser


def main(
    argv: list[str] | None = None, *, session_factory: Callable = SessionLocal,
    service_factory: Callable = OpportunityKnowledgeBriefService,
    probe_factory: Callable = probe_guts_model,
    output: TextIO = sys.stdout,
) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate-guts":
        return _generate_guts(
            args, session_factory=session_factory, service_factory=service_factory,
            output=output,
        )
    if args.command == "probe-guts-model":
        return _probe_guts(args, output=output, probe_factory=probe_factory)
    if args.command == "database-preflight":
        return _database_preflight(session_factory=session_factory, output=output)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
