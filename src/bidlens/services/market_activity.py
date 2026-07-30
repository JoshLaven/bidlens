from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import Opportunity, Vote


TIME_PERIODS = {
    "30_days": "Last 30 Days",
    "90_days": "Last 90 Days",
    "year_to_date": "Year to Date",
    "1_year": "Last 1 Year",
}
VIEW_BY_OPTIONS = {
    "account": "Account",
    "account_type": "Account Type",
    "naics": "NAICS",
}
METRIC_OPTIONS = {"count": "Count", "conversion": "Conversion %"}
SORT_COLUMNS = {"dimension", "imported", "qualified", "shortlisted"}
PAGE_SIZE = 10


@dataclass(frozen=True)
class MarketActivityFilters:
    start_date: date
    end_date: date


def market_period_dates(period: str, *, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if period == "30_days":
        return today - timedelta(days=29), today
    if period == "year_to_date":
        return date(today.year, 1, 1), today
    if period == "1_year":
        previous_year = today.year - 1
        previous_day = min(today.day, calendar.monthrange(previous_year, today.month)[1])
        return date(previous_year, today.month, previous_day) + timedelta(days=1), today
    return today - timedelta(days=89), today


def conversion_percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator * 100) / denominator, 1)


def _shortlisted_condition(organization_id: int):
    pursue_vote = exists(
        select(Vote.id).where(
            Vote.org_id == organization_id,
            Vote.opp_id == Opportunity.id,
            Vote.vote == "PURSUE",
        )
    )
    return or_(Opportunity.decision_state == "SHORTLISTED", pursue_vote)


def _qualified_condition(organization_id: int):
    return or_(
        Opportunity.qualification_status == "qualified",
        _shortlisted_condition(organization_id),
    )


def _base_conditions(organization_id: int, filters: MarketActivityFilters) -> list[Any]:
    start_at = datetime.combine(filters.start_date, time.min)
    end_before = datetime.combine(filters.end_date + timedelta(days=1), time.min)
    return [
        Opportunity.organization_id == organization_id,
        Opportunity.created_at >= start_at,
        Opportunity.created_at < end_before,
    ]


def _dimension_expression(view_by: str):
    if view_by == "account_type":
        return func.coalesce(func.nullif(func.trim(Opportunity.account_type), ""), "No Account Type")
    if view_by == "naics":
        return func.coalesce(func.nullif(func.trim(Opportunity.naics), ""), "No NAICS")
    return func.coalesce(func.nullif(func.trim(Opportunity.agency), ""), "Unassigned")


def _sort_rows(rows: list[dict[str, Any]], *, sort: str, direction: str, metric: str) -> None:
    reverse = direction == "desc"

    def value(row: dict[str, Any]):
        if sort == "dimension":
            return row["label"].casefold()
        if metric == "conversion":
            converted = row["conversion"][sort]
            return -1.0 if converted is None else converted
        return row[sort]

    rows.sort(key=value, reverse=reverse)


def build_market_activity(
    db: Session,
    *,
    organization_id: int,
    filters: MarketActivityFilters,
    view_by: str = "account",
    metric: str = "count",
    sort: str | None = None,
    direction: str = "desc",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> dict[str, Any]:
    view_by = view_by if view_by in VIEW_BY_OPTIONS else "account"
    metric = metric if metric in METRIC_OPTIONS else "count"
    sort = sort if sort in SORT_COLUMNS else ("qualified" if metric == "conversion" else "imported")
    direction = direction if direction in {"asc", "desc"} else "desc"
    qualified = _qualified_condition(organization_id)
    shortlisted = _shortlisted_condition(organization_id)
    conditions = _base_conditions(organization_id, filters)

    imported_total, qualified_total, shortlisted_total = db.query(
        func.count(Opportunity.id),
        func.sum(case((qualified, 1), else_=0)),
        func.sum(case((shortlisted, 1), else_=0)),
    ).filter(*conditions).one()
    metrics = {
        "imported": int(imported_total or 0),
        "qualified": int(qualified_total or 0),
        "shortlisted": int(shortlisted_total or 0),
    }
    metric_conversion = {
        "imported": 100.0 if metrics["imported"] else None,
        "qualified": conversion_percent(metrics["qualified"], metrics["imported"]),
        "shortlisted": conversion_percent(metrics["shortlisted"], metrics["qualified"]),
    }

    dimension = _dimension_expression(view_by).label("dimension")
    title = func.max(func.nullif(func.trim(Opportunity.naics_title), "")).label("naics_title")
    query_columns = [
        dimension,
        func.count(Opportunity.id).label("imported"),
        func.sum(case((qualified, 1), else_=0)).label("qualified"),
        func.sum(case((shortlisted, 1), else_=0)).label("shortlisted"),
    ]
    if view_by == "naics":
        query_columns.append(title)
    grouped = db.query(*query_columns).filter(*conditions).group_by(dimension).all()

    rows: list[dict[str, Any]] = []
    for grouped_row in grouped:
        code = str(grouped_row.dimension)
        naics_title = getattr(grouped_row, "naics_title", None)
        label = f"{code} — {naics_title}" if view_by == "naics" and code != "No NAICS" and naics_title else code
        imported = int(grouped_row.imported or 0)
        qualified_count = int(grouped_row.qualified or 0)
        shortlisted_count = int(grouped_row.shortlisted or 0)
        rows.append({
            "label": label,
            "imported": imported,
            "qualified": qualified_count,
            "shortlisted": shortlisted_count,
            "conversion": {
                "imported": 100.0 if imported else None,
                "qualified": conversion_percent(qualified_count, imported),
                "shortlisted": conversion_percent(shortlisted_count, qualified_count),
            },
        })

    _sort_rows(rows, sort=sort, direction=direction, metric=metric)
    page_size = max(1, int(page_size))
    total_rows = len(rows)
    total_pages = max(1, math.ceil(total_rows / page_size))
    page = min(max(1, int(page)), total_pages)
    start = (page - 1) * page_size

    return {
        "metrics": metrics,
        "metric_conversion": metric_conversion,
        "rows": rows[start : start + page_size],
        "total_rows": total_rows,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "view_by": view_by,
        "metric": metric,
        "sort": sort,
        "direction": direction,
    }
