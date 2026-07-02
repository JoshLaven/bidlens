from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import Event, Opportunity, Vote


@dataclass(frozen=True)
class MarketActivityFilters:
    start_date: date
    end_date: date
    source: str | None = None
    account_type: str | None = None
    category: str | None = None
    qualified_only: bool = False
    pushed_only: bool = False


def _qualified_condition(organization_id: int):
    pursue_vote = exists(
        select(Vote.id).where(
            Vote.org_id == organization_id,
            Vote.opp_id == Opportunity.id,
            Vote.vote == "PURSUE",
        )
    )
    return or_(
        Opportunity.qualification_status == "qualified",
        Opportunity.decision_state == "SHORTLISTED",
        pursue_vote,
    )


def _rejected_condition(organization_id: int):
    pass_vote = exists(
        select(Vote.id).where(
            Vote.org_id == organization_id,
            Vote.opp_id == Opportunity.id,
            Vote.vote == "PASS",
        )
    )
    return or_(
        Opportunity.qualification_status == "rejected",
        Opportunity.decision_state == "ARCHIVED",
        pass_vote,
    )


def _pushed_condition(organization_id: int):
    crm_event = exists(
        select(Event.id).where(
            Event.org_id == organization_id,
            Event.opp_id == Opportunity.id,
            Event.event_type == "crm_pushed",
        )
    )
    return or_(
        func.length(func.trim(Opportunity.salesforce_opportunity_id)) > 0,
        Opportunity.crm_pushed.is_(True),
        crm_event,
    )


def _naics_expression():
    return func.nullif(func.trim(Opportunity.naics), "")


def _account_type_expression():
    return func.coalesce(
        func.nullif(func.trim(Opportunity.account_type), ""),
        "__other__",
    )


def _month_expression(db: Session, column):
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return func.to_char(column, "YYYY-MM")
    return func.strftime("%Y-%m", column)


def _base_conditions(
    organization_id: int,
    filters: MarketActivityFilters,
) -> list[Any]:
    start_at = datetime.combine(filters.start_date, time.min)
    end_before = datetime.combine(filters.end_date + timedelta(days=1), time.min)
    conditions: list[Any] = [
        Opportunity.organization_id == organization_id,
        Opportunity.created_at >= start_at,
        Opportunity.created_at < end_before,
    ]
    if filters.source:
        conditions.append(Opportunity.source == filters.source)
    if filters.account_type == "__other__":
        conditions.append(
            or_(
                Opportunity.account_type.is_(None),
                func.trim(Opportunity.account_type) == "",
            )
        )
    elif filters.account_type:
        conditions.append(Opportunity.account_type == filters.account_type)
    if filters.category:
        conditions.append(Opportunity.naics == filters.category)
    if filters.qualified_only:
        conditions.append(_qualified_condition(organization_id))
    if filters.pushed_only:
        conditions.append(_pushed_condition(organization_id))
    return conditions


def _count(db: Session, conditions: list[Any], *additional: Any) -> int:
    return int(
        db.query(func.count(Opportunity.id))
        .filter(*conditions, *additional)
        .scalar()
        or 0
    )


def _month_label(key: str) -> str:
    try:
        return datetime.strptime(key, "%Y-%m").strftime("%b %Y")
    except (TypeError, ValueError):
        return key or "Unknown"


def build_market_activity(
    db: Session,
    *,
    organization_id: int,
    filters: MarketActivityFilters,
    today: date | None = None,
) -> dict[str, Any]:
    today = today or date.today()
    conditions = _base_conditions(organization_id, filters)
    qualified = _qualified_condition(organization_id)
    rejected = _rejected_condition(organization_id)
    pushed = _pushed_condition(organization_id)

    metrics = {
        "total": _count(db, conditions),
        "qualified": _count(db, conditions, qualified),
        "rejected": _count(db, conditions, rejected),
        "pushed": _count(db, conditions, pushed),
        "active_open": _count(
            db,
            conditions,
            Opportunity.response_deadline >= today,
            ~rejected,
        ),
    }

    import_month = _month_expression(db, Opportunity.created_at).label("month")
    monthly_rows = (
        db.query(
            import_month,
            func.count(Opportunity.id).label("imported"),
            func.sum(case((qualified, 1), else_=0)).label("qualified"),
        )
        .filter(*conditions)
        .group_by(import_month)
        .order_by(import_month)
        .all()
    )
    monthly = [
        {
            "key": month,
            "label": _month_label(month),
            "imported": int(imported or 0),
            "qualified": int(qualified_count or 0),
        }
        for month, imported, qualified_count in monthly_rows
        if month
    ]

    def grouped_rows(
        expression,
        *,
        limit: int | None = None,
        require_value: bool = False,
    ) -> list[dict[str, Any]]:
        label = expression.label("label")
        query = (
            db.query(label, func.count(Opportunity.id).label("count"))
            .filter(*conditions)
        )
        if require_value:
            query = query.filter(expression.isnot(None))
        query = query.group_by(label).order_by(func.count(Opportunity.id).desc(), label.asc())
        if limit:
            query = query.limit(limit)
        return [
            {"label": value or "Unknown", "count": int(count or 0)}
            for value, count in query.all()
        ]

    due_month = _month_expression(db, Opportunity.response_deadline).label("month")
    due_rows = (
        db.query(due_month, func.count(Opportunity.id).label("count"))
        .filter(
            *conditions,
            Opportunity.response_deadline >= today,
            ~rejected,
        )
        .group_by(due_month)
        .order_by(due_month)
        .all()
    )
    upcoming_due_dates = [
        {"key": month, "label": _month_label(month), "count": int(count or 0)}
        for month, count in due_rows
        if month
    ]
    by_source = grouped_rows(Opportunity.source)
    by_account_type = grouped_rows(_account_type_expression())
    top_agencies = grouped_rows(Opportunity.agency, limit=10)
    top_categories = grouped_rows(_naics_expression(), limit=10, require_value=True)

    return {
        "metrics": metrics,
        "monthly": monthly,
        "by_source": by_source,
        "by_account_type": by_account_type,
        "top_agencies": top_agencies,
        "top_categories": top_categories,
        "upcoming_due_dates": upcoming_due_dates,
        "max_imported": max((row["imported"] for row in monthly), default=0),
        "max_qualified": max((row["qualified"] for row in monthly), default=0),
        "max_source": max((row["count"] for row in by_source), default=0),
        "max_account_type": max((row["count"] for row in by_account_type), default=0),
        "max_agency": max((row["count"] for row in top_agencies), default=0),
        "max_category": max((row["count"] for row in top_categories), default=0),
        "max_due": max((row["count"] for row in upcoming_due_dates), default=0),
    }


def market_activity_filter_options(
    db: Session,
    *,
    organization_id: int,
) -> dict[str, list[str]]:
    base = [Opportunity.organization_id == organization_id]

    def distinct(column) -> list[str]:
        return [
            value
            for (value,) in (
                db.query(column)
                .filter(*base, column.isnot(None), func.trim(column) != "")
                .distinct()
                .order_by(column.asc())
                .all()
            )
        ]

    return {
        "sources": distinct(Opportunity.source),
        "account_types": distinct(Opportunity.account_type),
        "categories": distinct(Opportunity.naics),
    }
