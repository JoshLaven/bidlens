"""Official retained and provider-retrieved evidence for GUTS."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
from pathlib import PurePath
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from ... import config
from ...models import Opportunity, OpportunitySourceMaterial, Workspace
from ..opportunity_intake.storage import SourceMaterialStorage
from ..research.document_fetcher import fetch_opportunity_documents
from .constants import ExtractionStatus, FailureCategory
from .contracts import EvidenceSource, OfficialEvidenceCollectionResult, UnavailableSource
from .extraction_cache import DEFAULT_PARSER_NAME, DEFAULT_PARSER_VERSION, get_or_create_extraction
from .organizational_evidence import normalize_evidence_text


ALLOWED_MATERIAL_TYPES = {"rfp_document"}
ALLOWED_EXTERNAL_PROVIDERS = {"sam", "sam.gov", "grants_gov", "grants.gov"}


class OfficialEvidenceScopeError(ValueError):
    pass


def canonicalize_official_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host != "gov" and not host.endswith(".gov"):
        return None
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), host + port, path, query, ""))


def _bounded(text: str, maximum: int) -> tuple[str, bool]:
    if len(text) <= maximum:
        return text, False
    return text[: maximum - 1].rstrip() + "…", True


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class OfficialEvidenceCollector:
    def __init__(
        self, db: Session, *, storage: SourceMaterialStorage | None = None,
        external_fetcher: Callable[[Opportunity], dict] = fetch_opportunity_documents,
        maximum_documents: int = config.GUTS_MAX_OFFICIAL_DOCUMENTS,
        maximum_document_characters: int = config.GUTS_MAX_OFFICIAL_DOC_CHARS,
        maximum_total_characters: int = config.GUTS_MAX_TOTAL_OFFICIAL_CHARS,
    ):
        if min(maximum_documents, maximum_document_characters, maximum_total_characters) <= 0:
            raise ValueError("Official collector limits must be positive.")
        self.db = db
        self.storage = storage
        self.external_fetcher = external_fetcher
        self.maximum_documents = maximum_documents
        self.maximum_document_characters = maximum_document_characters
        self.maximum_total_characters = maximum_total_characters

    def collect(
        self, *, opportunity_id: int, organization_id: int, workspace_id: int,
    ) -> OfficialEvidenceCollectionResult:
        opportunity = self.db.get(Opportunity, opportunity_id)
        workspace = self.db.get(Workspace, workspace_id)
        if opportunity is None or opportunity.organization_id != organization_id:
            raise OfficialEvidenceScopeError("Opportunity is outside the requested organization scope.")
        if workspace is None or workspace.organization_id != organization_id:
            raise OfficialEvidenceScopeError("Workspace is outside the requested organization scope.")
        materials = self.db.query(OpportunitySourceMaterial).filter(
            OpportunitySourceMaterial.organization_id == organization_id,
            OpportunitySourceMaterial.workspace_id == workspace_id,
            OpportunitySourceMaterial.opportunity_id == opportunity_id,
        ).order_by(OpportunitySourceMaterial.created_at.asc(), OpportunitySourceMaterial.id.asc()).all()
        # Keep loaded scalar ORM state usable while closing the read transaction
        # before retained-object storage access and parsing.
        expire_on_commit = self.db.expire_on_commit
        self.db.expire_on_commit = False
        try:
            self.db.commit()
        finally:
            self.db.expire_on_commit = expire_on_commit
        candidates: list[EvidenceSource] = []
        unavailable: list[UnavailableSource] = []
        omitted = Counter()
        seen_hashes: set[str] = set()
        seen_urls: set[str] = set()
        seen_resource_ids: set[str] = set()
        seen_weak_identities: set[tuple[str, int]] = set()
        for material in materials:
            if material.material_type not in ALLOWED_MATERIAL_TYPES:
                omitted["retained_type_not_allowed"] += 1
                continue
            if PurePath(material.original_filename).suffix.lower() not in {".pdf", ".docx"}:
                unavailable.append(UnavailableSource(
                    source_id=f"source_material:{material.id}", source_type="solicitation_document",
                    failure_category="source_parse_failed", safe_message="The retained source format is unsupported.",
                    retryable=False, provenance={"internal_record_id": material.id},
                )); omitted["retained_unavailable"] += 1; continue
            extraction = get_or_create_extraction(
                self.db, source_material=material, organization_id=organization_id,
                workspace_id=workspace_id, opportunity_id=opportunity_id, storage=self.storage,
            )
            if extraction.status != ExtractionStatus.SUCCEEDED or not extraction.extracted_text:
                unavailable.append(UnavailableSource(
                    source_id=f"source_material:{material.id}", source_type="solicitation_document",
                    failure_category=extraction.failure_category or FailureCategory.SOURCE_PARSE_FAILED,
                    safe_message=extraction.safe_error_message or "The retained source could not be extracted.",
                    retryable=extraction.failure_category == FailureCategory.SOURCE_RETRIEVAL_FAILED,
                    provenance={"internal_record_id": material.id},
                )); omitted["retained_unavailable"] += 1; continue
            normalized = normalize_evidence_text(extraction.extracted_text)
            text, truncated = _bounded(normalized, self.maximum_document_characters)
            digest = _hash(text)
            if digest in seen_hashes:
                omitted["duplicate_official_content"] += 1; continue
            seen_hashes.add(digest)
            candidates.append(EvidenceSource(
                source_id=f"source_material:{material.id}", source_class="official_evidence",
                source_type="solicitation_document", authority="official_source",
                citation_label=f"{material.original_filename}, retained source",
                text=text, occurred_at=material.created_at, content_hash=digest,
                title=material.original_filename, internal_model_name="OpportunitySourceMaterial",
                internal_record_id=material.id, selected_character_count=len(text),
                original_character_count=len(normalized), was_truncated=truncated,
                verification="user_classified", provider=material.provider,
                filename=material.original_filename, parser_name=extraction.parser_name,
                parser_version=extraction.parser_version, retained_by_bidlens=True,
                provenance={
                    "organization_id": organization_id, "workspace_id": workspace_id,
                    "opportunity_id": opportunity_id, "byte_size": material.byte_size,
                    "page_count": extraction.page_count,
                },
            ))
        # Cache helpers commit their writes but refresh the returned row, which
        # begins a new read transaction. Close it before provider network I/O.
        self.db.commit()
        provider = str(opportunity.source or "").strip().lower()
        external_count = 0
        if provider in ALLOWED_EXTERNAL_PROVIDERS:
            try:
                result = self.external_fetcher(opportunity)
            except Exception:
                result = {"documents": [], "summary": {"total_attachments_found": 1, "extraction_failures": 1}}
            documents = result.get("documents") if isinstance(result, dict) else []
            summary = result.get("summary") if isinstance(result, dict) else {}
            documents = documents if isinstance(documents, list) else []
            summary = summary if isinstance(summary, dict) else {}
            external_count = max(len(documents), int(summary.get("total_attachments_found") or 0))
            document_failures = 0
            for document in documents:
                raw_url = document.get("source_url")
                canonical_url = canonicalize_official_url(raw_url)
                if not canonical_url:
                    omitted["external_url_not_allowed"] += 1
                    unavailable.append(UnavailableSource(
                        source_id=f"external_document:unavailable:{provider}:url:{document_failures + 1}",
                        source_type="solicitation_document", failure_category="source_retrieval_failed",
                        safe_message="An external document URL was outside the official provider allowlist.",
                        retryable=False, provenance={"provider": provider},
                    )); document_failures += 1; continue
                resource_id = str(document.get("resource_id") or "").strip()
                normalized = normalize_evidence_text(document.get("extracted_text"))
                if not normalized:
                    omitted["external_parse_failed"] += 1
                    unavailable.append(UnavailableSource(
                        source_id=f"external_document:unavailable:{provider}:parse:{document_failures + 1}",
                        source_type="solicitation_document", failure_category="source_parse_failed",
                        safe_message="An official provider document contained no readable supported text.",
                        retryable=False, provenance={"provider": provider},
                    )); document_failures += 1; continue
                text, truncated = _bounded(normalized, self.maximum_document_characters)
                digest = _hash(text)
                filename = str(document.get("filename") or "Official document").strip()[:180]
                byte_size = int(document.get("byte_size") or 0)
                weak_identity = (filename.casefold(), byte_size)
                if (
                    digest in seen_hashes or (resource_id and resource_id in seen_resource_ids)
                    or canonical_url in seen_urls or (byte_size > 0 and weak_identity in seen_weak_identities)
                ):
                    omitted["duplicate_official_content"] += 1; continue
                seen_hashes.add(digest); seen_urls.add(canonical_url)
                if resource_id: seen_resource_ids.add(resource_id)
                if byte_size > 0: seen_weak_identities.add(weak_identity)
                candidates.append(EvidenceSource(
                    source_id=f"external_document:sha256:{digest}", source_class="official_evidence",
                    source_type="amendment" if "amendment" in filename.casefold() else "solicitation_document",
                    authority="official_source", citation_label=f"{filename}, {provider}", text=text,
                    content_hash=digest, title=filename, selected_character_count=len(text),
                    original_character_count=len(normalized), was_truncated=truncated,
                    verification="provider_retrieved", provider=provider, source_url=canonical_url,
                    filename=filename, parser_name="opportunity_brief_document_fetcher",
                    parser_version="1", retained_by_bidlens=False,
                    provenance={
                        "organization_id": organization_id, "workspace_id": workspace_id,
                        "opportunity_id": opportunity_id,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    },
                ))
            failed = max(0, external_count - len(documents)) + int(summary.get("extraction_failures") or 0)
            # Fetcher summaries can overlap total-found and extraction-failure counts; cap to discovered count.
            failed = min(external_count, failed)
            for index in range(max(0, failed - document_failures)):
                unavailable.append(UnavailableSource(
                    source_id=f"external_document:unavailable:{provider}:{index + 1}",
                    source_type="solicitation_document", failure_category="source_retrieval_failed",
                    safe_message="An official provider document could not be retrieved or parsed.", retryable=True,
                    provenance={"provider": provider},
                )); omitted["external_unavailable"] += 1
        elif provider:
            omitted["external_provider_not_allowed"] += 1
        candidates.sort(key=lambda source: (
            0 if source.retained_by_bidlens else 1,
            source.occurred_at.isoformat() if source.occurred_at else "", source.source_id,
        ))
        selected: list[EvidenceSource] = []; used = 0
        for source in candidates:
            if len(selected) >= self.maximum_documents or used + len(source.text) > self.maximum_total_characters:
                omitted["official_limit"] += 1; continue
            selected.append(source); used += len(source.text)
        available_count = len(selected) + sum(omitted.values())
        excluded = available_count - len(selected)
        return OfficialEvidenceCollectionResult(
            evidence=tuple(selected), available_count=available_count, selected_count=len(selected),
            excluded_count=excluded, truncated=bool(excluded or any(item.was_truncated for item in selected)),
            omitted_reason_counts=dict(sorted(omitted.items())),
            latest_source_at=max((item.occurred_at for item in selected if item.occurred_at), default=None),
            total_selected_characters=sum(len(item.text) for item in selected),
            unavailable_sources=tuple(unavailable),
            contains_unretained_external=any(not item.retained_by_bidlens for item in selected),
        )
