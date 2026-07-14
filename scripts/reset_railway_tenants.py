#!/usr/bin/env python3
"""Dry-run and reset Railway tenant data.

This utility is intentionally production-architecture oriented:

- preserve schema and Alembic migration history
- preserve platform/system records such as plans and the BidLens Platform org
- remove tenant-scoped test/demo/customer data

The command is dry-run by default. To execute, source scripts/use-railway.sh
first, then pass both --execute and --confirm RESET_RAILWAY_TENANTS.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bidlens.auth import platform_admin_emails  # noqa: E402
from bidlens.config import DATABASE_URL, DATABASE_SCHEME, safe_database_url  # noqa: E402
from bidlens.database import Base, SessionLocal, engine  # noqa: E402
from bidlens.models import (  # noqa: E402
    CompanyProfile,
    DailySnapshot,
    DigestLog,
    Event,
    GrantsSourceConfig,
    IngestionRun,
    IngestionRunDetail,
    JobRun,
    Opportunity,
    OpportunityBrief,
    OpportunityHistoryEvent,
    OpportunityHistoryRecipient,
    OpportunityNote,
    OpportunityPursuitLaneMatch,
    OpportunityUpdateEvent,
    OrgProfile,
    Organization,
    OrganizationMembership,
    PursuitLane,
    PursuitLaneAssignment,
    SamSourceConfig,
    User,
    UserOpportunity,
    Vote,
    Workspace,
    WorkspaceInvitation,
)
import bidlens.models  # noqa: E402,F401 - ensure all model tables are registered


CONFIRMATION_PHRASE = "RESET_RAILWAY_TENANTS"


@dataclass
class ResetScope:
    protected_org_ids: set[int]
    protected_user_ids: set[int]
    tenant_org_ids: set[int]
    tenant_workspace_ids: set[int]
    tenant_user_ids: set[int]
    tenant_opportunity_ids: set[int]
    tenant_ingestion_run_ids: set[int]


@dataclass
class DeletePlanItem:
    label: str
    count: int
    delete: callable


def _require_railway_postgres_target() -> None:
    if DATABASE_SCHEME != "postgresql":
        raise SystemExit(
            "Refusing to reset tenant data: DATABASE_URL is not PostgreSQL. "
            f"Resolved target: {safe_database_url(DATABASE_URL)}"
        )
    try:
        url = make_url(DATABASE_URL)
    except Exception as exc:
        raise SystemExit("Refusing to reset tenant data: DATABASE_URL is not parseable.") from exc
    host = str(url.host or "").lower()
    if "rlwy.net" not in host and "railway" not in host:
        raise SystemExit(
            "Refusing to reset tenant data: target does not look like Railway. "
            "Set BIDLENS_ALLOW_NON_RAILWAY_RESET only after reviewing the script."
        )


def _ids(rows) -> set[int]:
    return {int(row[0]) for row in rows}


def _count(query) -> int:
    return int(query.count())


def _delete_query(query, *, execute: bool) -> int:
    count = _count(query)
    if execute and count:
        query.delete(synchronize_session=False)
    return count


def tenant_event_filter(scope: ResetScope):
    return Event.org_id.in_(scope.tenant_org_ids or {-1})


def preserved_event_filter(scope: ResetScope):
    return or_(
        Event.org_id.is_(None),
        Event.org_id.in_(scope.protected_org_ids or {-1}),
        Event.user_id.in_(scope.protected_user_ids or {-1}),
    )


def table_counts(session: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = session.execute(select(func.count()).select_from(table)).scalar_one()
    return counts


def build_scope(session: Session) -> ResetScope:
    protected_org_ids = _ids(
        session.query(Organization.id)
        .filter(or_(Organization.slug == "bidlens-platform", Organization.plan == "platform"))
        .all()
    )
    protected_user_ids = _ids(
        session.query(User.id)
        .outerjoin(OrganizationMembership, OrganizationMembership.user_id == User.id)
        .filter(
            or_(
                User.organization_id.in_(protected_org_ids or [-1]),
                OrganizationMembership.organization_id.in_(protected_org_ids or [-1]),
                User.email.in_(platform_admin_emails() or {""}),
            )
        )
        .all()
    )
    tenant_org_ids = _ids(
        session.query(Organization.id)
        .filter(~Organization.id.in_(protected_org_ids or [-1]))
        .all()
    )
    tenant_workspace_ids = _ids(
        session.query(Workspace.id)
        .filter(Workspace.organization_id.in_(tenant_org_ids or [-1]))
        .all()
    )
    tenant_user_ids = _ids(
        session.query(User.id)
        .filter(~User.id.in_(protected_user_ids or [-1]))
        .all()
    )
    tenant_opportunity_ids = _ids(
        session.query(Opportunity.id)
        .filter(Opportunity.organization_id.in_(tenant_org_ids or [-1]))
        .all()
    )
    tenant_ingestion_run_ids = _ids(
        session.query(IngestionRun.id)
        .filter(
            or_(
                IngestionRun.organization_id.is_(None),
                IngestionRun.organization_id.in_(tenant_org_ids or [-1]),
                IngestionRun.user_id.in_(tenant_user_ids or [-1]),
            )
        )
        .all()
    )
    return ResetScope(
        protected_org_ids=protected_org_ids,
        protected_user_ids=protected_user_ids,
        tenant_org_ids=tenant_org_ids,
        tenant_workspace_ids=tenant_workspace_ids,
        tenant_user_ids=tenant_user_ids,
        tenant_opportunity_ids=tenant_opportunity_ids,
        tenant_ingestion_run_ids=tenant_ingestion_run_ids,
    )


def build_delete_plan(session: Session, scope: ResetScope) -> list[DeletePlanItem]:
    tenant_org_ids = scope.tenant_org_ids or {-1}
    tenant_workspace_ids = scope.tenant_workspace_ids or {-1}
    tenant_user_ids = scope.tenant_user_ids or {-1}
    tenant_opportunity_ids = scope.tenant_opportunity_ids or {-1}
    tenant_ingestion_run_ids = scope.tenant_ingestion_run_ids or {-1}
    protected_org_ids = scope.protected_org_ids or {-1}
    protected_user_ids = scope.protected_user_ids or {-1}

    return [
        DeletePlanItem(
            "daily_snapshots",
            _count(session.query(DailySnapshot).filter(
                or_(
                    DailySnapshot.workspace_id.in_(tenant_workspace_ids),
                    DailySnapshot.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(DailySnapshot).filter(
                or_(
                    DailySnapshot.workspace_id.in_(tenant_workspace_ids),
                    DailySnapshot.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_history_recipients",
            _count(session.query(OpportunityHistoryRecipient).filter(
                or_(
                    OpportunityHistoryRecipient.organization_id.in_(tenant_org_ids),
                    OpportunityHistoryRecipient.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityHistoryRecipient.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityHistoryRecipient).filter(
                or_(
                    OpportunityHistoryRecipient.organization_id.in_(tenant_org_ids),
                    OpportunityHistoryRecipient.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityHistoryRecipient.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_update_events",
            _count(session.query(OpportunityUpdateEvent).filter(
                or_(
                    OpportunityUpdateEvent.organization_id.in_(tenant_org_ids),
                    OpportunityUpdateEvent.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityUpdateEvent.ingestion_run_id.in_(tenant_ingestion_run_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityUpdateEvent).filter(
                or_(
                    OpportunityUpdateEvent.organization_id.in_(tenant_org_ids),
                    OpportunityUpdateEvent.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityUpdateEvent.ingestion_run_id.in_(tenant_ingestion_run_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_history_events",
            _count(session.query(OpportunityHistoryEvent).filter(
                or_(
                    OpportunityHistoryEvent.organization_id.in_(tenant_org_ids),
                    OpportunityHistoryEvent.opportunity_id.in_(tenant_opportunity_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityHistoryEvent).filter(
                or_(
                    OpportunityHistoryEvent.organization_id.in_(tenant_org_ids),
                    OpportunityHistoryEvent.opportunity_id.in_(tenant_opportunity_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_briefs",
            _count(session.query(OpportunityBrief).filter(
                or_(
                    OpportunityBrief.organization_id.in_(tenant_org_ids),
                    OpportunityBrief.opportunity_id.in_(tenant_opportunity_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityBrief).filter(
                or_(
                    OpportunityBrief.organization_id.in_(tenant_org_ids),
                    OpportunityBrief.opportunity_id.in_(tenant_opportunity_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_notes",
            _count(session.query(OpportunityNote).filter(
                or_(
                    OpportunityNote.org_id.in_(tenant_org_ids),
                    OpportunityNote.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityNote.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityNote).filter(
                or_(
                    OpportunityNote.org_id.in_(tenant_org_ids),
                    OpportunityNote.opportunity_id.in_(tenant_opportunity_ids),
                    OpportunityNote.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "user_opportunities",
            _count(session.query(UserOpportunity).filter(
                or_(
                    UserOpportunity.organization_id.in_(tenant_org_ids),
                    UserOpportunity.opportunity_id.in_(tenant_opportunity_ids),
                    UserOpportunity.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(UserOpportunity).filter(
                or_(
                    UserOpportunity.organization_id.in_(tenant_org_ids),
                    UserOpportunity.opportunity_id.in_(tenant_opportunity_ids),
                    UserOpportunity.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "opportunity_pursuit_lane_matches",
            _count(session.query(OpportunityPursuitLaneMatch).filter(
                or_(
                    OpportunityPursuitLaneMatch.organization_id.in_(tenant_org_ids),
                    OpportunityPursuitLaneMatch.opportunity_id.in_(tenant_opportunity_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OpportunityPursuitLaneMatch).filter(
                or_(
                    OpportunityPursuitLaneMatch.organization_id.in_(tenant_org_ids),
                    OpportunityPursuitLaneMatch.opportunity_id.in_(tenant_opportunity_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "pursuit_lane_assignments",
            _count(session.query(PursuitLaneAssignment).filter(
                or_(
                    PursuitLaneAssignment.organization_id.in_(tenant_org_ids),
                    PursuitLaneAssignment.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(PursuitLaneAssignment).filter(
                or_(
                    PursuitLaneAssignment.organization_id.in_(tenant_org_ids),
                    PursuitLaneAssignment.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "votes",
            _count(session.query(Vote).filter(
                or_(
                    Vote.org_id.in_(tenant_org_ids),
                    Vote.opp_id.in_(tenant_opportunity_ids),
                    Vote.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(Vote).filter(
                or_(
                    Vote.org_id.in_(tenant_org_ids),
                    Vote.opp_id.in_(tenant_opportunity_ids),
                    Vote.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "digest_log",
            _count(session.query(DigestLog).filter(DigestLog.org_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(DigestLog).filter(DigestLog.org_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "ingestion_run_details",
            _count(session.query(IngestionRunDetail).filter(
                or_(
                    IngestionRunDetail.ingestion_run_id.in_(tenant_ingestion_run_ids),
                    IngestionRunDetail.matched_opportunity_id.in_(tenant_opportunity_ids),
                )
            )),
            lambda execute: _delete_query(session.query(IngestionRunDetail).filter(
                or_(
                    IngestionRunDetail.ingestion_run_id.in_(tenant_ingestion_run_ids),
                    IngestionRunDetail.matched_opportunity_id.in_(tenant_opportunity_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "company_profiles",
            _count(session.query(CompanyProfile).filter(CompanyProfile.org_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(CompanyProfile).filter(CompanyProfile.org_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "sam_source_configs",
            _count(session.query(SamSourceConfig).filter(SamSourceConfig.organization_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(SamSourceConfig).filter(SamSourceConfig.organization_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "grants_source_configs",
            _count(session.query(GrantsSourceConfig).filter(GrantsSourceConfig.organization_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(GrantsSourceConfig).filter(GrantsSourceConfig.organization_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "org_profiles",
            _count(session.query(OrgProfile).filter(OrgProfile.org_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(OrgProfile).filter(OrgProfile.org_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "pursuit_lanes",
            _count(session.query(PursuitLane).filter(PursuitLane.organization_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(PursuitLane).filter(PursuitLane.organization_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "workspace_invitations",
            _count(session.query(WorkspaceInvitation).filter(
                or_(
                    WorkspaceInvitation.organization_id.in_(tenant_org_ids),
                    WorkspaceInvitation.workspace_id.in_(tenant_workspace_ids),
                )
            )),
            lambda execute: _delete_query(session.query(WorkspaceInvitation).filter(
                or_(
                    WorkspaceInvitation.organization_id.in_(tenant_org_ids),
                    WorkspaceInvitation.workspace_id.in_(tenant_workspace_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "job_runs",
            _count(session.query(JobRun).filter(JobRun.organization_id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(JobRun).filter(JobRun.organization_id.in_(tenant_org_ids)), execute=execute),
        ),
        DeletePlanItem(
            "ingestion_runs",
            _count(session.query(IngestionRun).filter(IngestionRun.id.in_(tenant_ingestion_run_ids))),
            lambda execute: _delete_query(session.query(IngestionRun).filter(IngestionRun.id.in_(tenant_ingestion_run_ids)), execute=execute),
        ),
        DeletePlanItem(
            "events",
            _count(session.query(Event).filter(tenant_event_filter(scope))),
            lambda execute: _delete_query(
                session.query(Event).filter(tenant_event_filter(scope)),
                execute=execute,
            ),
        ),
        DeletePlanItem(
            "opportunities",
            _count(session.query(Opportunity).filter(Opportunity.id.in_(tenant_opportunity_ids))),
            lambda execute: _delete_query(session.query(Opportunity).filter(Opportunity.id.in_(tenant_opportunity_ids)), execute=execute),
        ),
        DeletePlanItem(
            "workspaces",
            _count(session.query(Workspace).filter(Workspace.id.in_(tenant_workspace_ids))),
            lambda execute: _delete_query(session.query(Workspace).filter(Workspace.id.in_(tenant_workspace_ids)), execute=execute),
        ),
        DeletePlanItem(
            "organization_memberships",
            _count(session.query(OrganizationMembership).filter(
                or_(
                    OrganizationMembership.organization_id.in_(tenant_org_ids),
                    OrganizationMembership.user_id.in_(tenant_user_ids),
                )
            )),
            lambda execute: _delete_query(session.query(OrganizationMembership).filter(
                or_(
                    OrganizationMembership.organization_id.in_(tenant_org_ids),
                    OrganizationMembership.user_id.in_(tenant_user_ids),
                )
            ), execute=execute),
        ),
        DeletePlanItem(
            "users",
            _count(session.query(User).filter(User.id.in_(tenant_user_ids))),
            lambda execute: _delete_query(session.query(User).filter(User.id.in_(tenant_user_ids)), execute=execute),
        ),
        DeletePlanItem(
            "organizations",
            _count(session.query(Organization).filter(Organization.id.in_(tenant_org_ids))),
            lambda execute: _delete_query(session.query(Organization).filter(Organization.id.in_(tenant_org_ids)), execute=execute),
        ),
    ]


def _print_scope(session: Session, scope: ResetScope) -> None:
    print("Protected platform/system records:")
    for org in session.query(Organization).filter(Organization.id.in_(scope.protected_org_ids or {-1})).order_by(Organization.id):
        print(f"  organization {org.id}: {org.name} ({org.slug}, plan={org.plan})")
    for user in session.query(User).filter(User.id.in_(scope.protected_user_ids or {-1})).order_by(User.id):
        print(f"  user {user.id}: {user.email}")

    print("Tenant organizations targeted for removal:")
    for org in session.query(Organization).filter(Organization.id.in_(scope.tenant_org_ids or {-1})).order_by(Organization.id):
        workspace = session.query(Workspace).filter(Workspace.organization_id == org.id).first()
        print(
            f"  organization {org.id}: {org.name} ({org.slug}, domain={org.email_domain}, "
            f"workspace_id={workspace.id if workspace else None})"
        )


def event_summary(session: Session, scope: ResetScope) -> dict[str, int]:
    tenant_events = _count(session.query(Event).filter(tenant_event_filter(scope)))
    platform_global_events = _count(session.query(Event).filter(preserved_event_filter(scope)))
    total_events = _count(session.query(Event))
    return {
        "tenant_events_to_delete": tenant_events,
        "platform_global_events_to_keep": platform_global_events,
        "other_events_to_keep": max(0, total_events - tenant_events - platform_global_events),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run or reset Railway tenant data.")
    parser.add_argument("--execute", action="store_true", help="Actually delete the planned tenant data.")
    parser.add_argument("--confirm", default="", help=f"Required phrase for --execute: {CONFIRMATION_PHRASE}")
    args = parser.parse_args()

    _require_railway_postgres_target()
    if args.execute and args.confirm != CONFIRMATION_PHRASE:
        raise SystemExit(f"Refusing to execute reset without --confirm {CONFIRMATION_PHRASE}")

    with SessionLocal() as session:
        scope = build_scope(session)
        plan = build_delete_plan(session, scope)
        print("BidLens Railway tenant reset")
        print(f"Database: {safe_database_url(DATABASE_URL)}")
        print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
        print()
        _print_scope(session, scope)
        print()
        events = event_summary(session, scope)
        print("Event rows:")
        print(f"  tenant events to delete: {events['tenant_events_to_delete']}")
        print(f"  platform/global events to keep: {events['platform_global_events_to_keep']}")
        print(f"  other events to keep: {events['other_events_to_keep']}")
        print()
        print("Rows planned for deletion:")
        for item in plan:
            print(f"  {item.label}: {item.count}")

        if not args.execute:
            print()
            print("Dry run only. No data was modified.")
            print(f"To execute: source scripts/use-railway.sh && python scripts/reset_railway_tenants.py --execute --confirm {CONFIRMATION_PHRASE}")
            return

        for item in plan:
            item.delete(True)
        session.commit()
        print()
        print("Railway tenant data reset complete.")


if __name__ == "__main__":
    main()
