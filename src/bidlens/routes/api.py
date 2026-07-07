from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Any, Optional
from ..database import get_db
from ..auth import get_current_user
from ..state_machine import OppState
from ..services import transition_state, cast_vote, push_opportunity_to_crm
from ..services.opportunity_history import (
    EVENT_SALESFORCE_SYNCHRONIZED,
    mark_history_read,
    record_history_event,
)
from ..services.salesforce import (
    PROSPECT_FEED_STATUS,
    SalesforceApiError,
    SalesforceConfigError,
    SalesforceService,
    generate_pkce_pair,
)
from ..models import CompanyProfile, Opportunity, OpportunityBrief, Organization, OrganizationMembership
from ..tenancy import current_org_id
from sqlalchemy import and_, or_
from datetime import date, datetime, timedelta
import html
import logging
import os
import re
import requests
import secrets
from .. import config
from ..services import get_vote_counts, get_vote_user_maps
from ..sam_client import _is_url_like
from ..services.research.brief_generator import (
    build_brief_request_payload,
    build_opportunity_source_text,
    generate_local_brief,
    generate_llm_brief,
)
from ..services.research.document_fetcher import fetch_opportunity_attachment_metadata
router = APIRouter(prefix="/api", tags=["api"])
logger = logging.getLogger(__name__)
_SALESFORCE_OAUTH_PKCE: dict[str, str] = {}
N8N_PROVIDER = "n8n"
N8N_MODEL = "n8n-webhook"
BRIEF_SECTION_KEYS = [
    "executive_summary",
    "key_dates",
    "buyer_agency",
    "scope_of_work",
    "eligibility_set_aside",
    "submission_requirements",
    "evaluation_criteria",
    "fit_signals",
    "risk_flags",
    "open_questions",
    "recommended_action",
]
QUALIFICATION_UNREVIEWED = "unreviewed"
QUALIFICATION_QUALIFIED = "qualified"
QUALIFICATION_REJECTED = "rejected"


def _normalize_brief_status(value: str | None) -> str:
    if value == "pending":
        return "generating"
    if value == "ok":
        return "completed"
    return value or "not_started"


def _best_description_text(opp: Opportunity) -> str:
    description_text = (opp.description_text or "").strip()
    if description_text:
        return description_text

    description = (opp.description or "").strip()
    if description and not _is_url_like(description):
        return description

    return ""


def _clean_preview_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _build_preview_payload(opp: Opportunity) -> dict[str, Any]:
    description = _clean_preview_text(_best_description_text(opp))
    if description:
        return {
            "ok": True,
            "state": "text",
            "title": opp.title,
            "description": description[:300] + ("…" if len(description) > 300 else ""),
            "sam_url": opp.sam_url,
            "source_url": opp.source_url or opp.sam_url,
        }

    if opp.source_url or opp.sam_url:
        return {
            "ok": True,
            "state": "sam_fallback",
            "title": opp.title,
            "description": "Detailed description available on SAM.gov",
            "sam_url": opp.sam_url,
            "source_url": opp.source_url or opp.sam_url,
        }

    return {
        "ok": True,
        "state": "empty",
        "title": opp.title,
        "description": "No description available.",
        "sam_url": None,
        "source_url": None,
    }


def _normalize_brief_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return [str(value).strip()] if str(value).strip() else []


def _normalize_n8n_brief_response(data: dict[str, Any], brief_payload: dict[str, Any]) -> dict[str, list[str]]:
    schema_keys = list((brief_payload.get("desired_brief_schema") or {}).keys()) or BRIEF_SECTION_KEYS
    brief_value = data.get("brief")
    normalized: dict[str, list[str]] = {key: ["Not found in available materials"] for key in schema_keys}

    if isinstance(brief_value, dict):
        legacy_map = {
            "executive_summary": _normalize_brief_items(brief_value.get("summary_bullets")),
            "scope_of_work": _normalize_brief_items(brief_value.get("deliverables")) + _normalize_brief_items(brief_value.get("key_requirements")),
            "eligibility_set_aside": _normalize_brief_items(brief_value.get("eligibility")),
            "submission_requirements": _normalize_brief_items(brief_value.get("key_requirements")),
            "risk_flags": _normalize_brief_items(brief_value.get("red_flags")),
            "open_questions": _normalize_brief_items(brief_value.get("recommended_next_steps")),
            "recommended_action": _normalize_brief_items(brief_value.get("recommended_next_steps")),
        }
        for key in schema_keys:
            items = _normalize_brief_items(brief_value.get(key))
            if not items and key in legacy_map:
                items = legacy_map[key]
            if items:
                normalized[key] = list(dict.fromkeys(items))
    elif isinstance(brief_value, str) and brief_value.strip():
        normalized["executive_summary"] = [brief_value.strip()]

    fit_signals = _normalize_brief_items(data.get("fit_signals"))
    if fit_signals:
        normalized["fit_signals"] = fit_signals

    risk_flags = _normalize_brief_items(data.get("risk_flags"))
    if risk_flags:
        normalized["risk_flags"] = risk_flags

    agency = (data.get("agency") or brief_payload.get("agency") or "").strip()
    if agency and normalized["buyer_agency"] == ["Not found in available materials"]:
        normalized["buyer_agency"] = [agency]

    response_deadline = (data.get("response_deadline") or brief_payload.get("response_deadline") or "").strip()
    if response_deadline and normalized["key_dates"] == ["Not found in available materials"]:
        normalized["key_dates"] = [f"Response deadline: {response_deadline}"]

    recommended_next_steps = _normalize_brief_items(data.get("recommended_next_steps"))
    if recommended_next_steps:
        if normalized["open_questions"] == ["Not found in available materials"]:
            normalized["open_questions"] = recommended_next_steps
        if normalized["recommended_action"] == ["Not found in available materials"]:
            normalized["recommended_action"] = recommended_next_steps

    return normalized


def _merge_n8n_source_metadata(n8n_data: dict[str, Any], brief_payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(brief_payload.get("source_summary") or {})

    direct_mappings = {
        "pdfs_processed": "pdfs_processed",
        "docs_processed": "docs_processed",
        "txts_processed": "txts_processed",
        "spreadsheets_skipped": "spreadsheets_skipped",
        "non_extractable_skipped": "non_extractable_skipped",
        "pages_extracted": "pages_extracted",
        "characters_read": "total_extracted_characters",
        "characters_sent_to_model": "characters_sent_to_model",
        "attachments_found": "total_attachments_found",
    }
    for source_key, summary_key in direct_mappings.items():
        value = n8n_data.get(source_key)
        if value is not None:
            merged[summary_key] = value

    document_filenames = n8n_data.get("document_filenames")
    if document_filenames:
        merged["document_filenames"] = document_filenames

    return merged


def _build_n8n_payload(opp: Opportunity, brief_payload: dict[str, Any]) -> dict[str, Any]:
    agency_parts = [part.strip().replace("_", " ") for part in (opp.agency or "").split(".") if part.strip()]
    department = agency_parts[0] if len(agency_parts) > 1 else None
    subagency = agency_parts[-1] if len(agency_parts) > 1 else None
    source_text, source_text_field = build_opportunity_source_text(
        opp,
        brief_context=brief_payload.get("brief_context") or brief_payload.get("text_for_enrichment"),
    )
    attachment_payload = fetch_opportunity_attachment_metadata(opp)
    description = (brief_payload.get("description") or source_text or "").strip()
    source_summary = dict(brief_payload.get("source_summary") or {})
    return {
        "opp_id": opp.id,
        "opportunity_id": opp.id,
        "bidlens_id": str(opp.bidlens_id) if getattr(opp, "bidlens_id", None) else None,
        "title": opp.title,
        "agency": opp.agency,
        "department": department,
        "subagency": subagency,
        "opportunity_type": opp.opportunity_type,
        "description": description,
        "description_text": description,
        "source_text": source_text,
        "source_text_field": source_text_field,
        "sam_url": opp.sam_url,
        "source": opp.source,
        "source_url": opp.source_url or opp.sam_url,
        "source_record_id": opp.source_record_id,
        "external_source_key": opp.external_source_key,
        "solicitation_number": opp.solicitation_number,
        "sam_notice_id": opp.sam_notice_id,
        "notice_id": opp.sam_notice_id,
        "response_deadline": brief_payload.get("response_deadline"),
        "posted_date": brief_payload.get("posted_date"),
        "naics": opp.naics,
        "naics_title": opp.naics_title,
        "set_aside": opp.set_aside,
        "attachments": attachment_payload.get("attachments", []),
        "attachment_count": len(attachment_payload.get("attachments", [])),
        "attachment_summary": attachment_payload.get("summary", {}),
        "document_filenames": brief_payload.get("filenames_processed", []),
        "text_for_brief": brief_payload.get("text_for_brief") or brief_payload.get("text_for_enrichment"),
        "brief_context": brief_payload.get("text_for_brief") or brief_payload.get("text_for_enrichment"),
        "pdfs_processed": source_summary.get("pdfs_processed", 0),
        "docs_processed": source_summary.get("docs_processed", 0),
        "txts_processed": source_summary.get("txts_processed", 0),
        "spreadsheets_skipped": source_summary.get("spreadsheets_skipped", 0),
        "non_extractable_skipped": source_summary.get("non_extractable_skipped", 0),
        "pages_extracted": source_summary.get("pages_extracted", 0),
        "characters_read": source_summary.get("total_extracted_characters", 0),
        "characters_sent_to_model": source_summary.get("characters_sent_to_model", 0),
        "attachments_found": source_summary.get("total_attachments_found", 0),
        "source_basis": brief_payload.get("source_basis"),
        "used_solicitation_documents": brief_payload.get("used_solicitation_documents"),
    }


class TransitionIn(BaseModel):
    opp_id: int
    to_state: str
    ui_version: str = "v1"
    archive_reason: Optional[str] = None


class CompanyProfileIn(BaseModel):
    company_name: Optional[str] = None
    website_url: Optional[str] = None
    cage_code: Optional[str] = None
    duns: Optional[str] = None
    uei: Optional[str] = None
    profile: Any


def require_user(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    setattr(user, "current_organization_id", current_org_id(request, db, user))
    if not getattr(user, "organization", None) or not user.organization.is_active:
        raise HTTPException(status_code=403, detail="Organization inactive")
    return user


def _user_org_id(user) -> int:
    return getattr(user, "current_organization_id", None) or user.organization_id


def _current_user_role(db: Session, user) -> str:
    membership = (
        db.query(OrganizationMembership)
        .filter(
            OrganizationMembership.organization_id == _user_org_id(user),
            OrganizationMembership.user_id == user.id,
        )
        .first()
    )
    return membership.role if membership else "member"


def require_admin(request: Request, db: Session):
    user = require_user(request, db)
    if _current_user_role(db, user) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    return user


def require_user_or_automation(request: Request, db: Session):
    expected = os.getenv("AUTOMATION_API_KEY")
    x_api_key = request.headers.get("x-api-key") or request.headers.get("X-Api-Key")

    if expected and x_api_key and x_api_key == expected:
        return {"automation": True}

    return require_user(request, db)


def _salesforce_redirect_uri(request: Request) -> str:
    if config.SALESFORCE_REDIRECT_URI:
        return config.SALESFORCE_REDIRECT_URI
    return str(request.url_for("salesforce_oauth_callback"))


def _salesforce_opportunity_name(opp: Opportunity) -> str:
    name = (opp.title or f"BidLens Opportunity {opp.id}").strip()
    if len(name) <= 120:
        return name
    return f"{name[:117]}..."


def _salesforce_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload)
    if summary.get("Description"):
        description = str(summary["Description"])
        summary["Description"] = description[:500] + ("..." if len(description) > 500 else "")
        summary["description_included"] = True
    return summary


def _record_salesforce_opportunity_reference(
    db: Session,
    *,
    opp: Opportunity,
    salesforce_opp_id: str,
    salesforce_opp_url: str,
    action: str,
) -> None:
    opp.salesforce_opportunity_id = salesforce_opp_id
    opp.salesforce_opportunity_url = salesforce_opp_url
    opp.salesforce_synced_at = datetime.utcnow()
    opp.salesforce_action = action
    db.commit()


def _record_salesforce_synchronized_history(
    db: Session,
    *,
    opp: Opportunity,
    salesforce_opp_id: str,
    action: str,
) -> None:
    record_history_event(
        db,
        opportunity=opp,
        event_type=EVENT_SALESFORCE_SYNCHRONIZED,
        source="salesforce",
        event_data={
            "salesforce_opportunity_id": salesforce_opp_id,
            "action": action,
        },
    )
    db.commit()


def _select_intake_source_value(values: list[str]) -> str | None:
    if "BidLens" in values:
        return "BidLens"
    return values[0] if values else None


class SalesforceCreateValidationError(ValueError):
    def __init__(self, detail: dict[str, Any]):
        super().__init__(str(detail.get("message") or "Salesforce create validation failed"))
        self.detail = detail


def _salesforce_create_payload(
    service: SalesforceService,
    opp: Opportunity,
) -> tuple[dict[str, Any], str, list[str]]:
    close_date = opp.response_deadline or (date.today() + timedelta(days=30))
    payload: dict[str, Any] = {
        "Name": _salesforce_opportunity_name(opp),
        "StageName": "Prospecting",
        "CloseDate": close_date.isoformat(),
        "External_Source_ID_c__c": (opp.source_record_id or "").strip(),
        "Intake_Status__c": PROSPECT_FEED_STATUS,
    }
    description = _best_description_text(opp)
    if description:
        payload["Description"] = description[:32000]

    required_fields = service.required_createable_opportunity_fields()
    valid_stage_names = service.stage_name_values()
    intake_source_values = service.opportunity_picklist_values("Intake_Source_c__c")
    selected_intake_source = _select_intake_source_value(intake_source_values)
    if "Prospecting" not in valid_stage_names:
        raise SalesforceCreateValidationError({
            "message": "Salesforce StageName 'Prospecting' is not valid in this org.",
            "valid_stage_names": valid_stage_names,
            "created": False,
        })
    if not selected_intake_source:
        raise SalesforceCreateValidationError({
            "message": "Salesforce Intake_Source_c__c has no active picklist values.",
            "intake_source_values": intake_source_values,
            "created": False,
        })
    payload["Intake_Source_c__c"] = selected_intake_source

    provided_fields = set(payload)
    missing_required_fields = [
        field
        for field in required_fields
        if field.get("name") and field["name"] not in provided_fields
    ]
    if missing_required_fields:
        raise SalesforceCreateValidationError({
            "message": "Salesforce Opportunity has required createable fields outside this POC payload.",
            "missing_required_fields": missing_required_fields,
            "required_fields": required_fields,
            "created": False,
        })
    return payload, selected_intake_source, intake_source_values


def _promote_interested_opportunity_to_salesforce(
    db: Session,
    *,
    user,
    opp: Opportunity,
    ui_version: str,
    service: SalesforceService | None = None,
) -> dict[str, Any]:
    source_record_id = (opp.source_record_id or "").strip()
    if not source_record_id:
        raise ValueError("Opportunity is missing source_record_id")

    service = service or SalesforceService()
    if not service.is_authorized():
        raise SalesforceConfigError("Salesforce is not connected.")

    sf_opp = None
    if opp.salesforce_opportunity_id:
        salesforce_opp_id = opp.salesforce_opportunity_id
        service.update_intake_status(salesforce_opp_id, PROSPECT_FEED_STATUS)
        salesforce_url = (
            opp.salesforce_opportunity_url
            or service.opportunity_record_url(salesforce_opp_id)
        )
        _record_salesforce_opportunity_reference(
            db,
            opp=opp,
            salesforce_opp_id=salesforce_opp_id,
            salesforce_opp_url=salesforce_url,
            action="pushed",
        )
        push_opportunity_to_crm(
            db,
            org_id=_user_org_id(user),
            user_id=user.id,
            opp_id=opp.id,
            ui_version=ui_version,
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opp,
            salesforce_opp_id=salesforce_opp_id,
            action="updated",
        )
        return {
            "outcome": "linked",
            "message": "Pushed to CRM · Updated in Salesforce",
            "salesforce_opportunity_id": salesforce_opp_id,
            "salesforce_opportunity_url": salesforce_url,
        }

    sf_opp = service.find_opportunity_by_external_source_id(source_record_id)
    if sf_opp:
        service.update_intake_status(sf_opp.id, PROSPECT_FEED_STATUS)
        salesforce_url = service.opportunity_record_url(sf_opp.id)
        _record_salesforce_opportunity_reference(
            db,
            opp=opp,
            salesforce_opp_id=sf_opp.id,
            salesforce_opp_url=salesforce_url,
            action="pushed",
        )
        push_opportunity_to_crm(
            db,
            org_id=_user_org_id(user),
            user_id=user.id,
            opp_id=opp.id,
            ui_version=ui_version,
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opp,
            salesforce_opp_id=sf_opp.id,
            action="updated",
        )
        return {
            "outcome": "linked",
            "message": "Pushed to CRM · Updated in Salesforce",
            "salesforce_opportunity_id": sf_opp.id,
            "salesforce_opportunity_url": salesforce_url,
        }

    payload, selected_intake_source, _intake_source_values = _salesforce_create_payload(
        service,
        opp,
    )
    salesforce_opp_id = service.create_opportunity(payload)
    salesforce_url = service.opportunity_record_url(salesforce_opp_id)
    _record_salesforce_opportunity_reference(
        db,
        opp=opp,
        salesforce_opp_id=salesforce_opp_id,
        salesforce_opp_url=salesforce_url,
        action="created",
    )
    push_opportunity_to_crm(
        db,
        org_id=_user_org_id(user),
        user_id=user.id,
        opp_id=opp.id,
        ui_version=ui_version,
    )
    _record_salesforce_synchronized_history(
        db,
        opp=opp,
        salesforce_opp_id=salesforce_opp_id,
        action="created",
    )
    return {
        "outcome": "created",
        "message": "Pushed to CRM · Created in Salesforce",
        "salesforce_opportunity_id": salesforce_opp_id,
        "salesforce_opportunity_url": salesforce_url,
        "selected_intake_source": selected_intake_source,
    }


def _clean_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _profile_text(profile: dict[str, Any], keys: list[str]) -> str | None:
    identifiers = profile.get("identifiers")
    sources = [profile]
    if isinstance(identifiers, dict):
        sources.append(identifiers)

    for source in sources:
        for key in keys:
            value = _clean_optional_text(source.get(key))
            if value:
                return value
    return None


def _normalized_company_name(value: str | None) -> str | None:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    return re.sub(r"\s+", " ", text) or None


def _company_profile_duplicate_matches(
    db: Session,
    *,
    org_id: int,
    company_name: str | None,
    cage_code: str | None,
    duns: str | None,
    uei: str | None,
    exclude_id: int | None = None,
) -> list[CompanyProfile]:
    normalized_name = _normalized_company_name(company_name)
    query = db.query(CompanyProfile).filter(
        CompanyProfile.org_id == org_id,
        CompanyProfile.archived_at.is_(None),
    )
    if exclude_id is not None:
        query = query.filter(CompanyProfile.id != exclude_id)

    matches = []
    for profile in query.all():
        identifier_match = any(
            [
                uei and profile.uei == uei,
                duns and profile.duns == duns,
                cage_code and profile.cage_code == cage_code,
            ]
        )
        name_match = normalized_name and _normalized_company_name(profile.company_name) == normalized_name
        if identifier_match or name_match:
            matches.append(profile)
    return matches


def _duplicate_warning_payload(matches: list[CompanyProfile]) -> dict[str, Any]:
    possible_duplicates = [
        {
            "id": profile.id,
            "company_name": profile.company_name or f"Company profile #{profile.id}",
            "uei": profile.uei,
            "duns": profile.duns,
            "cage_code": profile.cage_code,
        }
        for profile in matches[:5]
    ]
    warning = None
    if matches:
        labels = [f"{item['company_name']} (#{item['id']})" for item in possible_duplicates[:3]]
        extra = len(matches) - len(labels)
        suffix = f" and {extra} more" if extra > 0 else ""
        warning = f"Possible duplicate profile found: {', '.join(labels)}{suffix}."
    return {"duplicate_warning": warning, "possible_duplicates": possible_duplicates}


def _org_id_from_text(value: str | None, *, source: str, db: Session) -> int | None:
    if not value:
        return None
    try:
        org_id = int(value)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{source} must be an integer")

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=400, detail=f"{source} does not match an organization")
    return org.id


def _automation_org_id(request: Request, db: Session) -> int:
    query_org_id = request.query_params.get("org_id")
    org_id = _org_id_from_text(query_org_id, source="org_id", db=db)
    if org_id is not None:
        return org_id

    default_org_id = os.getenv("DEFAULT_ORGANIZATION_ID")
    org_id = _org_id_from_text(default_org_id, source="DEFAULT_ORGANIZATION_ID", db=db)
    if org_id is not None:
        return org_id

    raise HTTPException(
        status_code=400,
        detail="Automation requests must include ?org_id=123 or configure DEFAULT_ORGANIZATION_ID.",
    )


def _caller_org_id(caller, request: Request, db: Session) -> int:
    if isinstance(caller, dict) and caller.get("automation") is True:
        return _automation_org_id(request, db)
    return _user_org_id(caller)


@router.post("/company-profiles")
def api_save_company_profile(
    payload: CompanyProfileIn,
    request: Request,
    db: Session = Depends(get_db),
):
    caller = require_user_or_automation(request, db)

    if not isinstance(payload.profile, dict):
        raise HTTPException(status_code=400, detail="profile must be a JSON object")

    company_name = _clean_optional_text(payload.company_name) or _profile_text(payload.profile, ["company_name", "name", "legal_name"])
    website_url = _clean_optional_text(payload.website_url) or _profile_text(payload.profile, ["website_url", "website", "url"])
    cage_code = _clean_optional_text(payload.cage_code) or _profile_text(payload.profile, ["cage_code", "cage"])
    duns = _clean_optional_text(payload.duns) or _profile_text(payload.profile, ["duns", "duns_number"])
    uei = _clean_optional_text(payload.uei) or _profile_text(payload.profile, ["uei", "uei_sam"])

    org_id = _caller_org_id(caller, request, db)
    logger.info("Company profile save resolved organization_id=%s automation=%s", org_id, isinstance(caller, dict) and caller.get("automation") is True)
    duplicate_matches = _company_profile_duplicate_matches(
        db,
        org_id=org_id,
        company_name=company_name,
        cage_code=cage_code,
        duns=duns,
        uei=uei,
    )

    profile = CompanyProfile(
        org_id=org_id,
        company_name=company_name,
        website_url=website_url,
        cage_code=cage_code,
        duns=duns,
        uei=uei,
        profile_json=payload.profile,
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)

    return {
        "status": "saved",
        "id": profile.id,
        "company_name": profile.company_name,
        "organization_id": org_id,
        **_duplicate_warning_payload(duplicate_matches),
    }


@router.post("/company-profiles/{profile_id}/archive")
def api_archive_company_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)

    profile = (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.id == profile_id,
            CompanyProfile.org_id == org_id,
        )
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")

    if profile.archived_at is None:
        profile.archived_at = datetime.utcnow()
        db.commit()

    return {
        "ok": True,
        "id": profile.id,
        "organization_id": org_id,
        "archived_at": profile.archived_at.isoformat() if profile.archived_at else None,
    }


@router.delete("/company-profiles/{profile_id}")
def api_delete_company_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)

    profile = (
        db.query(CompanyProfile)
        .filter(
            CompanyProfile.id == profile_id,
            CompanyProfile.org_id == org_id,
        )
        .first()
    )
    if not profile:
        raise HTTPException(status_code=404, detail="Company profile not found")

    db.delete(profile)
    db.commit()
    return {"ok": True, "id": profile_id, "organization_id": org_id, "deleted": True}



@router.post("/transition")
def api_transition(payload: TransitionIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    try:
        to_state = OppState(payload.to_state)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid to_state")

    try:
        new_state = transition_state(
            db,
            org_id=_user_org_id(user),
            user_id=user.id,
            opp_id=payload.opp_id,
            to_state=to_state,
            ui_version=payload.ui_version,
            archive_reason=payload.archive_reason,
        )
        return {"ok": True, "opp_id": payload.opp_id, "state": new_state.value}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class VoteIn(BaseModel):
    opp_id: int
    vote: str  # "PURSUE" or "PASS"
    ui_version: str = "v1"


class BulkPassIn(BaseModel):
    opp_ids: list[int]
    ui_version: str = "v1"


class BulkQualificationIn(BaseModel):
    opp_ids: list[int]
    action: str


class CrmPushIn(BaseModel):
    opp_id: int
    ui_version: str = "v1"


def _serialize_sidebar(sidebar: dict) -> dict:
    def _serialize_items(items):
        out = []
        for opp in items:
            due_date = getattr(opp, "response_deadline", None)
            days_until_due = getattr(opp, "days_until_due", None)
            due_label = None
            if due_date and days_until_due is not None:
                due_label = f"Due {due_date.strftime('%b')} {due_date.day} ({days_until_due}D)"
            out.append({
                "id": opp.id,
                "title": opp.title,
                "days_until_due": getattr(opp, "days_until_due", None),
                "due_label": due_label,
                "salesforce_opportunity_url": getattr(opp, "salesforce_opportunity_url", None),
            })
        return out

    return {
        "my_shortlisted": _serialize_items(sidebar.get("my_shortlisted", [])),
        "following": _serialize_items(sidebar.get("following", [])),
    }


def _crm_workflow_response_payload(db: Session, user, opp_id: int) -> dict[str, Any]:
    counts = get_vote_counts(db, [opp_id]).get(opp_id, {"pursue": 0, "pass": 0})
    pursue_users_map, pass_users_map = get_vote_user_maps(
        db,
        org_id=_user_org_id(user),
        opp_ids=[opp_id],
    )
    from .opportunities import get_sidebar

    sidebar = get_sidebar(db, user)
    return {
        "pursue_count": counts["pursue"],
        "pass_count": counts["pass"],
        "pursue_users": pursue_users_map.get(opp_id, []),
        "pass_users": pass_users_map.get(opp_id, []),
        "in_my_shortlist": True,
        "sidebar": _serialize_sidebar(sidebar),
    }


@router.post("/vote")
def api_vote(payload: VoteIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    if payload.vote not in ("PURSUE", "PASS"):
        raise HTTPException(status_code=400, detail="vote must be PURSUE or PASS")

    try:
        result = cast_vote(
            db,
            org_id=_user_org_id(user),
            user_id=user.id,
            opp_id=payload.opp_id,
            vote=payload.vote,
            ui_version=payload.ui_version,
        )
        salesforce_result = {
            "outcome": "not_requested",
            "message": None,
            "salesforce_opportunity_id": None,
            "salesforce_opportunity_url": None,
        }
        is_admin_crm_action = (
            result["vote"] == "PURSUE"
            and _current_user_role(db, user) == "admin"
        )
        if is_admin_crm_action:
            opp = (
                db.query(Opportunity)
                .filter(
                    Opportunity.id == payload.opp_id,
                    Opportunity.organization_id == _user_org_id(user),
                )
                .one()
            )
            try:
                salesforce_result = _promote_interested_opportunity_to_salesforce(
                    db,
                    user=user,
                    opp=opp,
                    ui_version=payload.ui_version,
                )
            except Exception as exc:
                db.rollback()
                logger.warning(
                    "Interested Salesforce promotion failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s user_id=%s error=%s",
                    opp.id,
                    opp.source_record_id,
                    opp.external_source_key,
                    user.id,
                    str(exc),
                )
                salesforce_result = {
                    "outcome": "unavailable",
                    "message": "Push failed · Salesforce unavailable",
                    "warning": "Salesforce sync was not completed. The opportunity remains in My Shortlist.",
                    "error": str(exc),
                    "salesforce_opportunity_id": opp.salesforce_opportunity_id,
                    "salesforce_opportunity_url": opp.salesforce_opportunity_url,
                }
        workflow_payload = _crm_workflow_response_payload(db, user, payload.opp_id)

        return {
            "ok": True,
            **result,
            "opp_id": payload.opp_id,
            "in_my_shortlist": result["vote"] == "PURSUE",
            "pursue_count": workflow_payload["pursue_count"],
            "pass_count": workflow_payload["pass_count"],
            "pursue_users": workflow_payload["pursue_users"],
            "pass_users": workflow_payload["pass_users"],
            "sidebar": workflow_payload["sidebar"],
            "salesforce_outcome": salesforce_result["outcome"],
            "salesforce_message": salesforce_result.get("message"),
            "salesforce_warning": salesforce_result.get("warning"),
            "salesforce_error": salesforce_result.get("error"),
            "salesforce_opportunity_id": salesforce_result.get("salesforce_opportunity_id"),
            "salesforce_opportunity_url": salesforce_result.get("salesforce_opportunity_url"),
            "admin_crm_action": is_admin_crm_action,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/votes/bulk-pass")
def api_bulk_pass(payload: BulkPassIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    opp_ids = list(dict.fromkeys(payload.opp_ids))
    if not opp_ids:
        raise HTTPException(status_code=400, detail="Select at least one opportunity")
    if len(opp_ids) > 100:
        raise HTTPException(status_code=400, detail="Bulk archive is limited to 100 opportunities")

    org_id = _user_org_id(user)
    eligible_ids = {
        opp_id
        for (opp_id,) in (
            db.query(Opportunity.id)
            .filter(
                Opportunity.id.in_(opp_ids),
                Opportunity.organization_id == org_id,
                Opportunity.decision_state != "ARCHIVED",
                Opportunity.qualification_status == "qualified",
            )
            .all()
        )
    }
    invalid_ids = [opp_id for opp_id in opp_ids if opp_id not in eligible_ids]
    if invalid_ids:
        raise HTTPException(
            status_code=400,
            detail="One or more selected opportunities are unavailable",
        )

    try:
        for opp_id in opp_ids:
            cast_vote(
                db,
                org_id=org_id,
                user_id=user.id,
                opp_id=opp_id,
                vote="PASS",
                ui_version=payload.ui_version,
                toggle_existing=False,
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "ok": True,
        "archived_count": len(opp_ids),
        "archived_opp_ids": opp_ids,
    }


@router.post("/opportunities/push-crm")
def api_push_crm(payload: CrmPushIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    try:
        opp = push_opportunity_to_crm(
            db,
            org_id=_user_org_id(user),
            user_id=user.id,
            opp_id=payload.opp_id,
            ui_version=payload.ui_version,
        )
        workflow_payload = _crm_workflow_response_payload(db, user, payload.opp_id)
        return {
            "ok": True,
            "opp_id": opp.id,
            "crm_pushed": bool(opp.crm_pushed),
            "crm_pushed_by": opp.crm_pushed_by,
            "crm_pushed_by_current_user": opp.crm_pushed_by == user.id,
            **workflow_payload,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/salesforce/oauth/start")
def salesforce_oauth_start(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = generate_pkce_pair()
    _SALESFORCE_OAUTH_PKCE[state] = code_verifier
    service = SalesforceService()
    try:
        authorization_url = service.build_authorization_url(
            _salesforce_redirect_uri(request),
            state,
            code_challenge,
        )
    except SalesforceConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(authorization_url)


@router.get("/salesforce/oauth/callback", name="salesforce_oauth_callback")
def salesforce_oauth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail=f"Salesforce OAuth failed: {error}")
    if not code or not state or state not in _SALESFORCE_OAUTH_PKCE:
        raise HTTPException(status_code=400, detail="Invalid Salesforce OAuth callback")

    code_verifier = _SALESFORCE_OAUTH_PKCE.pop(state)
    service = SalesforceService()
    try:
        service.exchange_authorization_code(code, _salesforce_redirect_uri(request), code_verifier)
    except SalesforceConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except SalesforceApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return HTMLResponse(
        "<h1>Salesforce connected</h1>"
        "<p>BidLens can now query and update matching Salesforce Opportunities for this local POC.</p>"
    )


@router.get("/salesforce/opportunity-create-requirements")
def salesforce_opportunity_create_requirements(request: Request, db: Session = Depends(get_db)):
    require_user(request, db)
    service = SalesforceService()
    try:
        if not service.is_authorized():
            return {
                "auth_available": False,
                "required_fields": [],
                "valid_stage_names": [],
                "intake_source_values": [],
                "selected_intake_source": None,
                "error": "Salesforce is not authorized yet. Visit /api/salesforce/oauth/start first.",
            }
        return service.inspect_opportunity_requirements()
    except SalesforceConfigError as exc:
        return {
            "auth_available": False,
            "required_fields": [],
            "valid_stage_names": [],
            "intake_source_values": [],
            "selected_intake_source": None,
            "error": str(exc),
        }
    except SalesforceApiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Salesforce request failed: {exc}")


def _set_qualification_status(opp_id: int, status: str, request: Request, db: Session) -> dict[str, Any]:
    user = require_admin(request, db)
    opp = (
        db.query(Opportunity)
        .filter(
            Opportunity.id == opp_id,
            Opportunity.organization_id == _user_org_id(user),
        )
        .first()
    )
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    opp.qualification_status = status
    db.commit()
    return {
        "success": True,
        "opportunity_id": opp.id,
        "qualification_status": opp.qualification_status,
    }


@router.post("/opps/{opp_id}/qualify")
def qualify_opportunity(opp_id: int, request: Request, db: Session = Depends(get_db)):
    return _set_qualification_status(opp_id, QUALIFICATION_QUALIFIED, request, db)


@router.post("/opps/{opp_id}/history/read")
def read_opportunity_history(opp_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    opportunity = (
        db.query(Opportunity)
        .filter(
            Opportunity.id == opp_id,
            Opportunity.organization_id == _user_org_id(user),
        )
        .first()
    )
    if not opportunity:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    marked_read = mark_history_read(
        db,
        organization_id=_user_org_id(user),
        opportunity_id=opp_id,
        user_id=user.id,
    )
    return {"success": True, "marked_read": marked_read, "unread_count": 0}


@router.post("/opps/{opp_id}/reject")
def reject_opportunity(opp_id: int, request: Request, db: Session = Depends(get_db)):
    return _set_qualification_status(opp_id, QUALIFICATION_REJECTED, request, db)


@router.post("/opps/bulk-qualification")
def bulk_set_qualification_status(
    payload: BulkQualificationIn,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_admin(request, db)
    status_by_action = {
        "qualify": QUALIFICATION_QUALIFIED,
        "reject": QUALIFICATION_REJECTED,
    }
    status = status_by_action.get(payload.action)
    if status is None:
        raise HTTPException(status_code=400, detail="Invalid triage action")

    opp_ids = list(dict.fromkeys(payload.opp_ids))
    if not opp_ids:
        raise HTTPException(status_code=400, detail="Select at least one opportunity")
    if len(opp_ids) > 100:
        raise HTTPException(status_code=400, detail="Bulk triage is limited to 100 opportunities")

    org_id = _user_org_id(user)
    opportunities = (
        db.query(Opportunity)
        .filter(
            Opportunity.id.in_(opp_ids),
            Opportunity.organization_id == org_id,
            Opportunity.decision_state != "ARCHIVED",
            Opportunity.qualification_status == QUALIFICATION_UNREVIEWED,
        )
        .all()
    )
    opportunities_by_id = {opportunity.id: opportunity for opportunity in opportunities}
    if any(opp_id not in opportunities_by_id for opp_id in opp_ids):
        raise HTTPException(
            status_code=400,
            detail="One or more selected opportunities are no longer awaiting triage",
        )

    for opportunity in opportunities:
        opportunity.qualification_status = status
    db.commit()
    return {
        "success": True,
        "action": payload.action,
        "qualification_status": status,
        "updated_count": len(opp_ids),
        "updated_opportunity_ids": opp_ids,
    }


@router.post("/opps/{opp_id}/push-to-crm")
def api_push_opp_to_salesforce(opp_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    org_id = _user_org_id(user)
    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        logger.info(
            "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s success=false error=%s",
            opp_id,
            None,
            "Opportunity not found",
        )
        raise HTTPException(status_code=404, detail="Opportunity not found")

    source_record_id = (opp.source_record_id or "").strip()
    external_source_key = opp.external_source_key
    if not source_record_id:
        logger.info(
            "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s success=false error=%s",
            opp.id,
            source_record_id,
            external_source_key,
            "Opportunity is missing source_record_id",
        )
        raise HTTPException(status_code=400, detail="Opportunity is missing source_record_id")
    if opp.qualification_status != QUALIFICATION_QUALIFIED:
        raise HTTPException(status_code=400, detail="Opportunity must be qualified before CRM actions")
    if opp.decision_state == "ARCHIVED":
        raise HTTPException(status_code=400, detail="Cannot push archived opportunities to CRM")

    service = SalesforceService()
    salesforce_opp_id = None
    try:
        sf_opp = service.find_opportunity_by_external_source_id(source_record_id)
        if not sf_opp:
            message = (
                "No matching Salesforce Opportunity found for "
                f"External_Source_ID_c__c={source_record_id}"
            )
            logger.info(
                "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=false error=%s",
                opp.id,
                source_record_id,
                external_source_key,
                None,
                message,
            )
            raise HTTPException(status_code=404, detail=message)

        salesforce_opp_id = sf_opp.id
        previous_status = sf_opp.intake_status
        service.update_intake_status(sf_opp.id, PROSPECT_FEED_STATUS)
        salesforce_opp_url = service.opportunity_record_url(sf_opp.id)
        _record_salesforce_opportunity_reference(
            db,
            opp=opp,
            salesforce_opp_id=sf_opp.id,
            salesforce_opp_url=salesforce_opp_url,
            action="pushed",
        )
        push_opportunity_to_crm(
            db,
            org_id=org_id,
            user_id=user.id,
            opp_id=opp.id,
            ui_version="v1",
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opp,
            salesforce_opp_id=sf_opp.id,
            action="updated",
        )
        workflow_payload = _crm_workflow_response_payload(db, user, opp.id)
        logger.info(
            "Salesforce CRM push succeeded bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=true",
            opp.id,
            source_record_id,
            external_source_key,
            sf_opp.id,
        )
        return {
            "success": True,
            "bidlens_opportunity_id": opp.id,
            "source_record_id": source_record_id,
            "external_source_key": external_source_key,
            "salesforce_opportunity_id": sf_opp.id,
            "salesforce_opportunity_url": salesforce_opp_url,
            "salesforce_linked": True,
            "salesforce_action": "pushed",
            "salesforce_status": "Pushed to Salesforce",
            "salesforce_opportunity_name": sf_opp.name,
            "previous_intake_status": previous_status,
            "new_intake_status": PROSPECT_FEED_STATUS,
            **workflow_payload,
        }
    except HTTPException:
        raise
    except SalesforceConfigError as exc:
        logger.info(
            "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=false error=%s",
            opp.id,
            source_record_id,
            external_source_key,
            salesforce_opp_id,
            str(exc),
        )
        raise HTTPException(status_code=500, detail=str(exc))
    except SalesforceApiError as exc:
        logger.info(
            "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=false error=%s",
            opp.id,
            source_record_id,
            external_source_key,
            salesforce_opp_id,
            str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.RequestException as exc:
        logger.info(
            "Salesforce CRM push failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=false error=%s",
            opp.id,
            source_record_id,
            external_source_key,
            salesforce_opp_id,
            str(exc),
        )
        raise HTTPException(status_code=502, detail="Salesforce request failed")


@router.post("/opps/{opp_id}/create-in-crm")
def api_create_opp_in_salesforce(opp_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    org_id = _user_org_id(user)
    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    source_record_id = (opp.source_record_id or "").strip()
    if not source_record_id:
        raise HTTPException(status_code=400, detail="Opportunity is missing source_record_id")
    if opp.qualification_status != QUALIFICATION_QUALIFIED:
        raise HTTPException(status_code=400, detail="Opportunity must be qualified before CRM actions")
    if opp.decision_state == "ARCHIVED":
        raise HTTPException(status_code=400, detail="Cannot create archived opportunities in CRM")

    service = SalesforceService()
    try:
        if not service.is_authorized():
            raise HTTPException(
                status_code=401,
                detail="Salesforce is not authorized yet. Visit /api/salesforce/oauth/start first.",
            )

        try:
            payload, selected_intake_source, intake_source_values = (
                _salesforce_create_payload(service, opp)
            )
        except SalesforceCreateValidationError as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc

        salesforce_opp_id = service.create_opportunity(payload)
        salesforce_opp_url = service.opportunity_record_url(salesforce_opp_id)
        _record_salesforce_opportunity_reference(
            db,
            opp=opp,
            salesforce_opp_id=salesforce_opp_id,
            salesforce_opp_url=salesforce_opp_url,
            action="created",
        )
        push_opportunity_to_crm(
            db,
            org_id=org_id,
            user_id=user.id,
            opp_id=opp.id,
            ui_version="v1",
        )
        _record_salesforce_synchronized_history(
            db,
            opp=opp,
            salesforce_opp_id=salesforce_opp_id,
            action="created",
        )
        workflow_payload = _crm_workflow_response_payload(db, user, opp.id)
        logger.info(
            "Salesforce Opportunity create succeeded bidlens_opp_id=%s source_record_id=%s external_source_key=%s salesforce_opp_id=%s success=true",
            opp.id,
            source_record_id,
            opp.external_source_key,
            salesforce_opp_id,
        )
        return {
            "success": True,
            "created": True,
            "bidlens_opportunity_id": opp.id,
            "source_record_id": source_record_id,
            "external_source_key": opp.external_source_key,
            "salesforce_opportunity_id": salesforce_opp_id,
            "salesforce_opportunity_url": salesforce_opp_url,
            "salesforce_linked": True,
            "salesforce_action": "created",
            "salesforce_status": "Created in Salesforce",
            "selected_intake_source": selected_intake_source,
            "intake_source_values": intake_source_values,
            "created_payload_summary": _salesforce_payload_summary(payload),
            **workflow_payload,
        }
    except HTTPException:
        raise
    except SalesforceConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except SalesforceApiError as exc:
        logger.info(
            "Salesforce Opportunity create failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s success=false error=%s",
            opp.id,
            source_record_id,
            opp.external_source_key,
            str(exc),
        )
        raise HTTPException(status_code=502, detail=str(exc))
    except requests.RequestException as exc:
        logger.info(
            "Salesforce Opportunity create failed bidlens_opp_id=%s source_record_id=%s external_source_key=%s success=false error=%s",
            opp.id,
            source_record_id,
            opp.external_source_key,
            str(exc),
        )
        raise HTTPException(status_code=502, detail="Salesforce request failed")


@router.get("/opps/{opp_id}/preview")
def opportunity_preview(
    opp_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)

    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == _user_org_id(user),
    ).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return _build_preview_payload(opp)


REVIEW_STAGES = ["Team Review", "Director Review", "Approved"]

# Allowed stage transitions: current_stage -> set of valid next stages
STAGE_TRANSITIONS = {
    "Team Review":     {"Director Review"},
    "Director Review": {"Approved", "Team Review"},  # can approve or return
    "Approved":        set(),                         # terminal
}


class StageIn(BaseModel):
    opp_id: int
    stage: str


@router.post("/stage")
def api_set_stage(payload: StageIn, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)

    if payload.stage not in REVIEW_STAGES:
        raise HTTPException(status_code=400, detail=f"Invalid stage. Must be one of: {REVIEW_STAGES}")

    opp = db.query(Opportunity).filter(
        Opportunity.id == payload.opp_id,
        Opportunity.organization_id == _user_org_id(user),
    ).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    if opp.decision_state != "SHORTLISTED":
        raise HTTPException(status_code=400, detail="Stage only applies to interested opportunities")

    current = opp.review_stage or "Team Review"
    allowed = STAGE_TRANSITIONS.get(current, set())
    if payload.stage not in allowed:
        raise HTTPException(status_code=400, detail=f"Cannot move from '{current}' to '{payload.stage}'")

    opp.review_stage = payload.stage
    opp.stage_changed_at = datetime.utcnow()
    opp.stage_changed_by = user.id
    db.commit()

    return {"ok": True, "opp_id": payload.opp_id, "stage": payload.stage}


@router.get("/opps/pending_enrichment")
def pending_enrichment(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    if isinstance(caller, dict) and caller.get("automation") is True:
        pass
    else:
        raise HTTPException(status_code=401, detail="Automation only endpoint")
    org_id = _caller_org_id(caller, request, db)
    logger.info("Pending enrichment resolved organization_id=%s", org_id)

    # Pending work = no brief row OR brief.status in not_started/generating/failed
    q = (
        db.query(Opportunity)
        .outerjoin(
            OpportunityBrief,
            and_(
                OpportunityBrief.opportunity_id == Opportunity.id,
                OpportunityBrief.organization_id == org_id,
            ),
        )
        .filter(Opportunity.organization_id == org_id)
        .filter(
            or_(
                OpportunityBrief.id.is_(None),
                OpportunityBrief.status.in_(["not_started", "generating", "failed", "pending"])
            )
        )
        .order_by(Opportunity.response_deadline.asc())
        .limit(limit)
    )

    opps = q.all()

    out = []
    for o in opps:
        # give n8n the text it needs
        text_for_enrichment = _best_description_text(o)
        out.append({
            "id": o.id,
            "organization_id": org_id,
            "title": o.title,
            "agency": o.agency,
            "opportunity_type": o.opportunity_type,
            "posted_date": o.posted_date.isoformat() if o.posted_date else None,
            "response_deadline": o.response_deadline.isoformat() if o.response_deadline else None,
            "naics": o.naics,
            "naics_title": o.naics_title,
            "set_aside": o.set_aside,
            "url": o.source_url or o.sam_url,
            "source": o.source,
            "source_url": o.source_url or o.sam_url,
            "source_record_id": o.source_record_id,
            "external_source_key": o.external_source_key,
            "solicitation_number": o.solicitation_number,
            "sam_notice_id": o.sam_notice_id,
            "text_for_brief": text_for_enrichment[:20000],  # guardrail
            "text_for_enrichment": text_for_enrichment[:20000],  # backward compatibility
        })
    return out
    
class BriefIn(BaseModel):
    brief: dict
    provider: Optional[str] = None
    model: Optional[str] = None
    source_basis: Optional[str] = None
    sources_used: Optional[list[dict[str, Any]]] = None
    filenames_processed: Optional[list[str]] = None
    source_summary: Optional[dict[str, Any]] = None


def _apply_brief_source_metadata(row: OpportunityBrief, payload: dict[str, Any]) -> None:
    row.provider = payload.get("provider")
    row.source_basis = payload.get("source_basis")
    row.sources_used = payload.get("sources_used")
    row.filenames_processed = payload.get("filenames_processed")
    row.source_summary = payload.get("source_summary")

@router.post("/opps/{opp_id}/enrichment")
def save_enrichment(opp_id: int, payload: BriefIn, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)
    logger.info("Enrichment save resolved organization_id=%s opp_id=%s", org_id, opp_id)
    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.organization_id == org_id,
        OpportunityBrief.opportunity_id == opp_id,
    ).first()
    if not row:
        row = OpportunityBrief(organization_id=org_id, opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.brief_json = payload.brief
    row.provider = payload.provider
    row.model = payload.model
    row.generated_at = datetime.utcnow()
    row.status = "completed"
    row.error_message = None
    if payload.source_basis or payload.sources_used or payload.filenames_processed or payload.source_summary:
        _apply_brief_source_metadata(
            row,
            {
                "source_basis": payload.source_basis,
                "sources_used": payload.sources_used,
                "filenames_processed": payload.filenames_processed,
                "source_summary": payload.source_summary,
            },
        )

    db.commit()
    return {"ok": True, "opp_id": opp_id, "organization_id": org_id}
    
@router.post("/opps/{opp_id}/enrichment/reset")
def reset_enrichment(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.organization_id == org_id,
        OpportunityBrief.opportunity_id == opp_id,
    ).first()
    if not row:
        row = OpportunityBrief(organization_id=org_id, opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.status = "not_started"
    row.error_message = None
    row.brief_json = None
    row.model = None
    row.provider = None
    row.generated_at = None
    row.source_basis = None
    row.sources_used = None
    row.filenames_processed = None
    row.source_summary = None

    db.commit()
    return {"ok": True, "opp_id": opp_id, "organization_id": org_id, "status": "not_started"}
    
@router.post("/opps/{opp_id}/mark_pending")
def mark_pending(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.organization_id == org_id,
        OpportunityBrief.opportunity_id == opp_id,
    ).first()
    if not row:
        row = OpportunityBrief(organization_id=org_id, opportunity_id=opp_id)
        db.add(row)
        db.flush()

    row.status = "generating"
    row.error_message = None
    db.commit()
    return {"ok": True, "opp_id": opp_id, "organization_id": org_id, "status": "generating"}

@router.post("/opps/{opp_id}/generate_brief")
def generate_brief(
    opp_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    logger.info("Generate brief requested opp_id=%s user_id=%s", opp_id, getattr(user, "id", None))

    org_id = _user_org_id(user)
    opp = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not opp:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.organization_id == org_id,
        OpportunityBrief.opportunity_id == opp_id
    ).first()

    if not row:
        row = OpportunityBrief(
            organization_id=org_id,
            opportunity_id=opp_id,
            status="generating",
        )
        db.add(row)
    else:
        row.status = "generating"
        row.error_message = None

    brief_payload = build_brief_request_payload(opp)
    _apply_brief_source_metadata(row, brief_payload)
    db.commit()

    fallback_triggered = False
    n8n_url = os.getenv("N8N_BRIEF_WEBHOOK_URL")
    if n8n_url:
        n8n_payload = _build_n8n_payload(opp, brief_payload)
        source_text = (n8n_payload.get("source_text") or "").strip()
        source_text_field = n8n_payload.get("source_text_field")
        description = (n8n_payload.get("description") or "").strip()
        if not source_text:
            logger.warning(
                "No local SAM description available for brief generation opp_id=%s title=%s description_length=%s source_text_length=%s source_text_field=%s",
                opp_id,
                opp.title,
                len(description),
                len(source_text),
                source_text_field,
            )
            logger.info("Skipping n8n brief generation for opp_id=%s and falling back to direct OpenAI/local flow", opp_id)
        else:
            logger.info(
                "Attempting n8n brief generation opp_id=%s title=%s description_length=%s source_text_length=%s source_text_field=%s attachment_count=%s webhook=%s",
                opp_id,
                opp.title,
                len(description),
                len(source_text),
                source_text_field,
                n8n_payload.get("attachment_count", 0),
                n8n_url,
            )
        try:
            if source_text:
                n8n_response = requests.post(
                    n8n_url,
                    json=n8n_payload,
                    timeout=60,
                )
                n8n_response.raise_for_status()
                n8n_data = n8n_response.json()
                row.brief_json = _normalize_n8n_brief_response(n8n_data, brief_payload)
                row.provider = N8N_PROVIDER
                row.model = n8n_data.get("model") or N8N_MODEL
                row.generated_at = datetime.utcnow()
                row.status = "completed"
                row.error_message = None
                row.source_basis = n8n_data.get("source_basis") or brief_payload.get("source_basis")
                row.filenames_processed = n8n_data.get("document_filenames") or brief_payload.get("filenames_processed")
                row.source_summary = _merge_n8n_source_metadata(n8n_data, brief_payload)
                db.commit()
                logger.info(
                    "Generate brief finished opp_id=%s provider=%s model=%s fallback_triggered=%s",
                    opp_id,
                    row.provider,
                    row.model,
                    fallback_triggered,
                )
                return {
                    "ok": True,
                    "status": "ok",
                    "source_basis": brief_payload["source_basis"],
                    "filenames_processed": brief_payload["filenames_processed"],
                    "provider": row.provider,
                    "model": row.model,
                    "fallback_triggered": fallback_triggered,
                }
        except Exception as exc:
            logger.warning("n8n brief generation failed for opp_id=%s; falling back to direct OpenAI error=%s", opp_id, repr(exc))

    try:
        llm_result = generate_llm_brief(brief_payload)
        row.brief_json = llm_result["brief"]
        row.provider = llm_result["provider"]
        row.model = llm_result["model"]
        row.generated_at = datetime.utcnow()
        row.status = "completed"
        row.error_message = None
    except Exception as exc:
        fallback_triggered = True
        logger.warning("OpenAI brief generation failed for opp_id=%s; using local fallback error=%s", opp_id, repr(exc))
        try:
            row.brief_json = generate_local_brief(opp, brief_payload)
            row.provider = "local"
            row.model = "local-deterministic-fallback"
            row.generated_at = datetime.utcnow()
            row.status = "completed"
            row.error_message = None
            db.commit()
            return {
                "ok": True,
                "status": "completed",
                "source_basis": brief_payload["source_basis"],
                "filenames_processed": brief_payload["filenames_processed"],
                "provider": row.provider,
                "model": row.model,
                "fallback_triggered": fallback_triggered,
            }
        except Exception as fallback_exc:
            row.status = "failed"
            row.error_message = f"Brief generation failed: {fallback_exc}"
            db.commit()
            logger.exception("Brief generation failed completely opp_id=%s error=%s", opp_id, repr(fallback_exc))
            raise HTTPException(status_code=500, detail="Brief generation failed")

    db.commit()
    logger.info(
        "Generate brief finished opp_id=%s provider=%s model=%s fallback_triggered=%s",
        opp_id,
        row.provider,
        row.model,
        fallback_triggered,
    )

    return {
        "ok": True,
        "status": "completed",
        "source_basis": brief_payload["source_basis"],
        "filenames_processed": brief_payload["filenames_processed"],
        "provider": row.provider,
        "model": row.model,
        "fallback_triggered": fallback_triggered,
    }
    
@router.get("/opps/{opp_id}")
def get_opp_for_enrichment(opp_id: int, request: Request, db: Session = Depends(get_db)):
    caller = require_user_or_automation(request, db)
    org_id = _caller_org_id(caller, request, db)
    o = db.query(Opportunity).filter(
        Opportunity.id == opp_id,
        Opportunity.organization_id == org_id,
    ).first()
    if not o:
        raise HTTPException(status_code=404, detail="Not found")

    payload = build_brief_request_payload(o)

    row = db.query(OpportunityBrief).filter(
        OpportunityBrief.organization_id == org_id,
        OpportunityBrief.opportunity_id == opp_id,
    ).first()
    if not row:
        row = OpportunityBrief(organization_id=org_id, opportunity_id=opp_id)
        db.add(row)
        db.flush()
    _apply_brief_source_metadata(row, payload)
    db.commit()

    payload["organization_id"] = org_id
    return payload
