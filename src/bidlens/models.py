from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, ForeignKey, Enum, Index, text
from sqlalchemy.orm import relationship
import enum
from .database import Base
from sqlalchemy import UniqueConstraint
from sqlalchemy import JSON, false, func, true
from sqlalchemy import BigInteger
import uuid
from sqlalchemy import TypeDecorator
import platform

# Use native PG UUID when available, fallback to String(36) for SQLite
class PortableUUID(TypeDecorator):
    impl = String
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid.UUID(value) if not isinstance(value, uuid.UUID) else value
        return value


class OpportunityStatus(str, enum.Enum):
    SAVED = "saved"
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"

class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("organization_id", "source", "source_record_id", name="uq_opportunity_org_source_record"),
    )

    # internal DB PK (keep)
    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    # platform/public ID (new)
    bidlens_id = Column(
        PortableUUID(),
        default=uuid.uuid4,
        unique=True,
        nullable=False,
        index=True
    )

    source = Column(String, nullable=False, default="sam", server_default="sam", index=True)
    source_record_id = Column(String, nullable=False, index=True)
    solicitation_number = Column(String, nullable=True, index=True)
    source_url = Column(String, nullable=True)
    raw_source_payload = Column(JSON, nullable=True)

    sam_notice_id = Column(String, nullable=True, index=True)
    govwin_staging_id = Column(String, nullable=True, index=True)

    title = Column(String, nullable=False)
    agency = Column(String, nullable=False)
    opportunity_type = Column(String, nullable=False)
    source_stage = Column(String, nullable=True, index=True)
    posted_date = Column(Date, nullable=False)
    response_deadline = Column(Date, nullable=False)
    naics = Column(String, nullable=True)
    naics_title = Column(String, nullable=True)
    set_aside = Column(String, nullable=True)
    account_type = Column(String, nullable=True)
    account_type_confidence = Column(String, nullable=True)
    account_type_source = Column(String, nullable=True)
    qualification_status = Column(String, nullable=False, default="unreviewed", server_default="unreviewed", index=True)
    description = Column(Text, nullable=True)
    description_url = Column(Text, nullable=True)
    description_text = Column(Text, nullable=True)
    sam_url = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    upserted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)
    last_seen_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)

    # Org-level decision state: INBOX → SHORTLISTED or ARCHIVED
    decision_state = Column(String, nullable=False, default="INBOX", server_default="INBOX", index=True)

    # Review stage within SHORTLISTED (Team Review → Director Review → Approved)
    review_stage = Column(String, nullable=True, default=None)
    stage_changed_at = Column(DateTime, nullable=True)
    stage_changed_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Archive metadata (populated when decision_state moves to ARCHIVED)
    archived_reason = Column(String, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    archived_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    # Local v1 CRM promotion marker. This is intentionally not an external CRM
    # integration; it records that BidLens users promoted the opportunity.
    crm_pushed = Column(Boolean, nullable=False, default=False, server_default=false(), index=True)
    crm_pushed_at = Column(DateTime, nullable=True)
    crm_pushed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    salesforce_opportunity_id = Column(String, nullable=True, index=True)
    salesforce_opportunity_url = Column(String, nullable=True)
    salesforce_synced_at = Column(DateTime, nullable=True)
    salesforce_action = Column(String, nullable=True)

    @property
    def external_source_key(self) -> str | None:
        source = str(self.source or "").strip()
        source_record_id = str(self.source_record_id or "").strip()
        if not source or not source_record_id:
            return None
        return f"{source}:{source_record_id}"

    user_opportunities = relationship("UserOpportunity", back_populates="opportunity")
    notes = relationship("OpportunityNote", back_populates="opportunity", cascade="all, delete-orphan")
    pursuit_lane_matches = relationship("OpportunityPursuitLaneMatch", back_populates="opportunity", cascade="all, delete-orphan")
    update_events = relationship(
        "OpportunityUpdateEvent",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )
    history_events = relationship(
        "OpportunityHistoryEvent",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )
    conversations = relationship(
        "OpportunityConversation",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )
    activity_events = relationship(
        "OpportunityActivityEvent",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )
    communication_summary = relationship(
        "OpportunityCommunicationSummary",
        back_populates="opportunity",
        cascade="all, delete-orphan",
        uselist=False,
    )
    outcomes = relationship(
        "OpportunityOutcome",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )
    knowledge_brief_generations = relationship(
        "OpportunityKnowledgeBriefGeneration",
        back_populates="opportunity",
        cascade="all, delete-orphan",
    )


class OpportunityKnowledgeBriefGeneration(Base):
    __tablename__ = "opportunity_knowledge_brief_generations"
    __table_args__ = (
        Index(
            "ix_guts_generation_org_opp_status_completed",
            "organization_id", "opportunity_id", "status", "completed_at",
        ),
        Index(
            "ix_guts_generation_opp_status_requested",
            "opportunity_id", "status", "requested_at",
        ),
        Index("ix_guts_generation_manifest_hash", "manifest_hash"),
        Index("ix_guts_generation_user_requested", "generated_by_user_id", "requested_at"),
        Index(
            "uq_guts_generation_active_org_opp",
            "organization_id", "opportunity_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True)
    generated_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    prompt_version = Column(String, nullable=False)
    manifest_version = Column(String, nullable=False)
    output_schema_version = Column(String, nullable=False)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)

    manifest_hash = Column(String(64), nullable=True)
    source_snapshot_started_at = Column(DateTime(timezone=True), nullable=True)
    source_snapshot_completed_at = Column(DateTime(timezone=True), nullable=True)
    latest_source_at = Column(DateTime(timezone=True), nullable=True)
    current_state_snapshot_json = Column(JSON, nullable=True)
    source_summary_json = Column(JSON, nullable=True)
    warning_metadata_json = Column(JSON, nullable=True)
    statistics_json = Column(JSON, nullable=True)
    output_json = Column(JSON, nullable=True)

    input_character_count = Column(Integer, nullable=True)
    estimated_input_tokens = Column(Integer, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    validation_retry_count = Column(Integer, nullable=False, default=0, server_default="0")

    authorization_ms = Column(Integer, nullable=True)
    current_state_ms = Column(Integer, nullable=True)
    official_evidence_ms = Column(Integer, nullable=True)
    communication_ms = Column(Integer, nullable=True)
    notes_ms = Column(Integer, nullable=True)
    history_ms = Column(Integer, nullable=True)
    manifest_ms = Column(Integer, nullable=True)
    model_ms = Column(Integer, nullable=True)
    validation_ms = Column(Integer, nullable=True)
    persistence_ms = Column(Integer, nullable=True)
    total_ms = Column(Integer, nullable=True)

    reproducibility_status = Column(String, nullable=False, default="not_reproducible", server_default="not_reproducible")
    degraded_source_count = Column(Integer, nullable=False, default=0, server_default="0")
    input_truncated = Column(Boolean, nullable=False, default=False, server_default=false())

    failure_category = Column(String, nullable=True)
    failure_stage = Column(String, nullable=True)
    safe_error_message = Column(String(500), nullable=True)

    organization = relationship("Organization")
    workspace = relationship("Workspace")
    opportunity = relationship("Opportunity", back_populates="knowledge_brief_generations")
    generated_by_user = relationship("User", foreign_keys=[generated_by_user_id])
    statements = relationship(
        "OpportunityKnowledgeBriefStatement",
        back_populates="generation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OpportunityKnowledgeBriefStatement.position",
    )
    sources = relationship(
        "OpportunityKnowledgeBriefSource",
        back_populates="generation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OpportunityKnowledgeBriefSource.id",
    )


class OpportunityKnowledgeBriefStatement(Base):
    __tablename__ = "opportunity_knowledge_brief_statements"
    __table_args__ = (
        UniqueConstraint("generation_id", "statement_key", name="uq_guts_statement_generation_key"),
        Index(
            "ix_guts_statement_generation_placement_section_position",
            "generation_id", "placement_type", "section_type", "position",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    generation_id = Column(Integer, ForeignKey("opportunity_knowledge_brief_generations.id", ondelete="CASCADE"), nullable=False, index=True)
    statement_key = Column(String, nullable=False)
    placement_type = Column(String, nullable=False)
    section_type = Column(String, nullable=True)
    section_title = Column(String, nullable=True)
    position = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    importance = Column(String, nullable=False)
    confidence = Column(String, nullable=False)
    attribution_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    generation = relationship("OpportunityKnowledgeBriefGeneration", back_populates="statements")
    source_links = relationship(
        "OpportunityKnowledgeBriefStatementSource",
        back_populates="statement",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="OpportunityKnowledgeBriefStatementSource.position",
    )


class OpportunityKnowledgeBriefSource(Base):
    __tablename__ = "opportunity_knowledge_brief_sources"
    __table_args__ = (
        UniqueConstraint("generation_id", "source_id", name="uq_guts_source_generation_source_id"),
        Index("ix_guts_source_generation_class", "generation_id", "source_class"),
        Index("ix_guts_source_internal_record", "internal_model_name", "internal_record_id"),
        Index("ix_guts_source_content_hash", "content_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    generation_id = Column(Integer, ForeignKey("opportunity_knowledge_brief_generations.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id = Column(String, nullable=False)
    source_class = Column(String, nullable=False)
    source_type = Column(String, nullable=False)
    authority = Column(String, nullable=False)
    verification = Column(String, nullable=True)
    title = Column(String, nullable=True)
    citation_label = Column(String, nullable=False)
    author_display_name = Column(String, nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    author_address = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    occurred_at = Column(DateTime(timezone=True), nullable=True)
    effective_at = Column(DateTime(timezone=True), nullable=True)
    updated_at_source = Column(DateTime(timezone=True), nullable=True)
    internal_model_name = Column(String, nullable=True)
    internal_record_id = Column(String, nullable=True)
    source_url = Column(Text, nullable=True)
    filename = Column(String, nullable=True)
    content_hash = Column(String(64), nullable=True)
    parser_name = Column(String, nullable=True)
    parser_version = Column(String, nullable=True)
    retained_by_bidlens = Column(Boolean, nullable=False, default=False, server_default=false())
    selected_character_count = Column(Integer, nullable=True)
    original_character_count = Column(Integer, nullable=True)
    was_truncated = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    generation = relationship("OpportunityKnowledgeBriefGeneration", back_populates="sources")
    author_user = relationship("User", foreign_keys=[author_user_id])
    statement_links = relationship(
        "OpportunityKnowledgeBriefStatementSource",
        back_populates="brief_source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OpportunityKnowledgeBriefStatementSource(Base):
    __tablename__ = "opportunity_knowledge_brief_statement_sources"
    __table_args__ = (
        UniqueConstraint("statement_id", "brief_source_id", name="uq_guts_statement_source_link"),
        Index("ix_guts_statement_source_brief_source", "brief_source_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    statement_id = Column(Integer, ForeignKey("opportunity_knowledge_brief_statements.id", ondelete="CASCADE"), nullable=False, index=True)
    brief_source_id = Column(Integer, ForeignKey("opportunity_knowledge_brief_sources.id", ondelete="CASCADE"), nullable=False)
    position = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    statement = relationship("OpportunityKnowledgeBriefStatement", back_populates="source_links")
    brief_source = relationship("OpportunityKnowledgeBriefSource", back_populates="statement_links")


class OpportunityIntakeDraft(Base):
    __tablename__ = "opportunity_intake_drafts"
    __table_args__ = (
        UniqueConstraint("publish_idempotency_key", name="uq_opportunity_intake_publish_idempotency"),
        UniqueConstraint("internal_reference", name="uq_opportunity_intake_internal_reference"),
        Index("ix_opportunity_intake_drafts_org_workspace_status", "organization_id", "workspace_id", "status"),
        Index("ix_opportunity_intake_drafts_workspace_creator", "workspace_id", "created_by_user_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    intake_method = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="DRAFT", server_default="DRAFT", index=True)
    candidate_fields_json = Column(JSON, nullable=False, default=dict)
    extraction_metadata_json = Column(JSON, nullable=True)
    validation_errors_json = Column(JSON, nullable=True)
    add_to_shortlist = Column(Boolean, nullable=False, default=True, server_default=true())
    published_opportunity_id = Column(Integer, ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True)
    publish_idempotency_key = Column(String, nullable=True)
    internal_reference = Column(String, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    organization = relationship("Organization")
    workspace = relationship("Workspace")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    published_opportunity = relationship("Opportunity", foreign_keys=[published_opportunity_id])
    source_materials = relationship(
        "OpportunitySourceMaterial",
        back_populates="intake_draft",
        passive_deletes=True,
    )


class OpportunitySourceMaterial(Base):
    __tablename__ = "opportunity_source_materials"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_opportunity_source_material_storage_key"),
        Index("ix_opportunity_source_materials_workspace_draft", "workspace_id", "intake_draft_id"),
        Index("ix_opportunity_source_materials_workspace_opportunity", "workspace_id", "opportunity_id"),
        Index("ix_opportunity_source_materials_workspace_sha256", "workspace_id", "sha256_digest"),
        Index("ix_opportunity_source_materials_workspace_provider_message", "workspace_id", "provider_message_id"),
        Index("ix_opportunity_source_materials_workspace_internet_message", "workspace_id", "internet_message_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    intake_draft_id = Column(Integer, ForeignKey("opportunity_intake_drafts.id", ondelete="SET NULL"), nullable=True, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True)
    parent_material_id = Column(Integer, ForeignKey("opportunity_source_materials.id", ondelete="SET NULL"), nullable=True, index=True)
    material_type = Column(String, nullable=False, index=True)
    original_filename = Column(String, nullable=False)
    mime_type = Column(String, nullable=True)
    byte_size = Column(BigInteger, nullable=False)
    sha256_digest = Column(String(64), nullable=False, index=True)
    storage_key = Column(String, nullable=False)
    provider = Column(String, nullable=True)
    provider_metadata_json = Column(JSON, nullable=True)
    provider_message_id = Column(String, nullable=True, index=True)
    internet_message_id = Column(String, nullable=True, index=True)
    parsed_metadata_json = Column(JSON, nullable=True)
    parse_status = Column(String, nullable=False, default="PENDING", server_default="PENDING", index=True)
    parse_error_code = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organization = relationship("Organization")
    workspace = relationship("Workspace")
    intake_draft = relationship("OpportunityIntakeDraft", back_populates="source_materials")
    opportunity = relationship("Opportunity", foreign_keys=[opportunity_id])
    parent_material = relationship("OpportunitySourceMaterial", remote_side=[id], back_populates="child_materials")
    child_materials = relationship("OpportunitySourceMaterial", back_populates="parent_material", passive_deletes=True)
    extractions = relationship(
        "OpportunitySourceMaterialExtraction",
        back_populates="source_material",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class OpportunitySourceMaterialExtraction(Base):
    __tablename__ = "opportunity_source_material_extractions"
    __table_args__ = (
        UniqueConstraint(
            "source_material_id", "content_hash", "parser_name", "parser_version",
            name="uq_source_material_extraction_cache_key",
        ),
        Index("ix_source_material_extraction_material_status", "source_material_id", "status"),
        Index("ix_source_material_extraction_content_hash", "content_hash"),
        Index("ix_source_material_extraction_parser", "parser_name", "parser_version"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source_material_id = Column(
        Integer,
        ForeignKey("opportunity_source_materials.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    content_hash = Column(String(64), nullable=False)
    parser_name = Column(String, nullable=False)
    parser_version = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    extracted_text = Column(Text, nullable=True)
    character_count = Column(Integer, nullable=True)
    page_count = Column(Integer, nullable=True)
    warnings_json = Column(JSON, nullable=True)
    failure_category = Column(String, nullable=True)
    safe_error_message = Column(String(500), nullable=True)
    extracted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    source_material = relationship("OpportunitySourceMaterial", back_populates="extractions")


class OpportunityOutcome(Base):
    __tablename__ = "opportunity_outcomes"
    __table_args__ = (
        UniqueConstraint("organization_id", "opportunity_id", name="uq_opportunity_outcome_org_opp"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    outcome_type = Column(String, nullable=False, index=True)
    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reason_code = Column(String, nullable=True)
    reason_text = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    opportunity = relationship("Opportunity", back_populates="outcomes")
    organization = relationship("Organization")
    recorded_by_user = relationship("User")


class OpportunityConversation(Base):
    __tablename__ = "opportunity_conversations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_conversation_id",
            name="uq_opportunity_conversation_workspace_provider_external",
        ),
        Index("ix_opportunity_conversations_workspace_opportunity", "workspace_id", "opportunity_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, default="manual", server_default="manual", index=True)
    external_conversation_id = Column(String, nullable=True, index=True)
    subject = Column(String, nullable=False)
    started_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    participant_summary = Column(Text, nullable=True)
    message_count = Column(Integer, nullable=False, default=0, server_default="0")
    first_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    send_status = Column(String, nullable=True, index=True)
    send_requested_at = Column(DateTime(timezone=True), nullable=True)
    accepted_for_delivery_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    uncertain_at = Column(DateTime(timezone=True), nullable=True)
    send_error_code = Column(String, nullable=True)
    idempotency_key_digest = Column(String, nullable=True, unique=True, index=True)
    idempotency_expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    recipient_count = Column(Integer, nullable=True)
    provider_mailbox_id = Column(String, nullable=True, index=True)
    initial_provider_message_id = Column(String, nullable=True)
    tracking_status = Column(String, nullable=True, index=True)
    last_successful_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_attempted_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_provider_message_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="opportunity_conversations")
    opportunity = relationship("Opportunity", back_populates="conversations")
    started_by_user = relationship("User")
    activity_events = relationship(
        "OpportunityActivityEvent",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )
    messages = relationship(
        "OpportunityCommunicationMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )


class OpportunityCommunicationMessage(Base):
    __tablename__ = "opportunity_communication_messages"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "provider",
            "provider_mailbox_id",
            "provider_message_id",
            name="uq_opp_comm_message_workspace_provider_mailbox_message",
        ),
        Index(
            "ix_opp_comm_message_workspace_opportunity_timestamp",
            "workspace_id",
            "opportunity_id",
            "provider_timestamp",
        ),
        Index(
            "ix_opp_comm_message_workspace_provider_conversation",
            "workspace_id",
            "provider",
            "provider_conversation_id",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("opportunity_conversations.id"), nullable=False, index=True)
    associated_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    provider = Column(String, nullable=False, index=True)
    direction = Column(String, nullable=False, index=True)
    provider_mailbox_id = Column(String, nullable=True, index=True)
    provider_message_id = Column(String, nullable=True, index=True)
    provider_conversation_id = Column(String, nullable=True, index=True)
    internet_message_id = Column(String, nullable=True, index=True)
    sender_address = Column(String, nullable=True)
    sender_display_name = Column(String, nullable=True)
    recipients_json = Column(JSON, nullable=True)
    cc_recipients_json = Column(JSON, nullable=True)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    body_content_type = Column(String, nullable=True)
    provider_timestamp = Column(DateTime(timezone=True), nullable=True, index=True)
    provider_web_link = Column(Text, nullable=True)
    imported_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    conversation = relationship("OpportunityConversation", back_populates="messages")
    workspace = relationship("Workspace", back_populates="opportunity_communication_messages")
    opportunity = relationship("Opportunity")
    associated_user = relationship("User")


class OpportunityCommunicationSummary(Base):
    __tablename__ = "opportunity_communication_summaries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "opportunity_id", name="uq_opp_comm_summary_workspace_opportunity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    status = Column(String, nullable=False, default="ready", server_default="ready", index=True)
    current_status = Column(Text, nullable=True)
    key_updates_json = Column(JSON, nullable=True)
    open_questions_json = Column(JSON, nullable=True)
    next_action = Column(Text, nullable=True)
    waiting_on = Column(String, nullable=True)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    message_count_included = Column(Integer, nullable=False, default=0, server_default="0")
    message_count_available = Column(Integer, nullable=False, default=0, server_default="0")
    latest_message_timestamp_included = Column(DateTime(timezone=True), nullable=True)
    generated_at = Column(DateTime(timezone=True), nullable=True)
    generated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    opportunity = relationship("Opportunity", back_populates="communication_summary")
    workspace = relationship("Workspace")
    organization = relationship("Organization")
    generated_by_user = relationship("User")


class OpportunityConversationSendAttempt(Base):
    __tablename__ = "opportunity_conversation_send_attempts"
    __table_args__ = (
        UniqueConstraint("idempotency_key_digest", name="uq_opp_conversation_send_attempt_idempotency"),
        Index("ix_opp_conversation_send_attempt_scope", "workspace_id", "opportunity_id", "user_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("opportunity_conversations.id"), nullable=True, index=True)
    idempotency_key_digest = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    recipient_count = Column(Integer, nullable=False, default=0, server_default="0")
    error_code = Column(String, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")
    opportunity = relationship("Opportunity")
    user = relationship("User")
    conversation = relationship("OpportunityConversation")


class OpportunityActivityEvent(Base):
    __tablename__ = "opportunity_activity_events"
    __table_args__ = (
        Index(
            "ix_opportunity_activity_workspace_opportunity_occurred",
            "workspace_id",
            "opportunity_id",
            "occurred_at",
        ),
        Index(
            "ix_opportunity_activity_opportunity_occurred",
            "opportunity_id",
            "occurred_at",
        ),
        Index(
            "ix_opportunity_activity_conversation_occurred",
            "conversation_id",
            "occurred_at",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    conversation_id = Column(Integer, ForeignKey("opportunity_conversations.id"), nullable=True, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    metadata_json = Column(JSON, nullable=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    workspace = relationship("Workspace", back_populates="opportunity_activity_events")
    opportunity = relationship("Opportunity", back_populates="activity_events")
    actor = relationship("User")
    conversation = relationship("OpportunityConversation", back_populates="activity_events")


class OpportunityUpdateEvent(Base):
    __tablename__ = "opportunity_update_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    ingestion_run_id = Column(Integer, ForeignKey("ingestion_runs.id"), nullable=True, index=True)
    source = Column(String, nullable=False, index=True)
    source_record_id = Column(String, nullable=False, index=True)
    detected_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    changed_fields = Column(JSON, nullable=False, default=dict)
    salesforce_payload = Column(JSON, nullable=True)
    salesforce_response = Column(JSON, nullable=True)
    salesforce_sync_status = Column(String, nullable=False)
    salesforce_synced_at = Column(DateTime(timezone=True), nullable=True)
    salesforce_error = Column(Text, nullable=True)

    opportunity = relationship("Opportunity", back_populates="update_events")
    ingestion_run = relationship("IngestionRun", back_populates="update_events")


class OpportunityHistoryEvent(Base):
    __tablename__ = "opportunity_history_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    source = Column(String, nullable=True, index=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    event_data = Column(JSON, nullable=True)

    opportunity = relationship("Opportunity", back_populates="history_events")
    recipients = relationship(
        "OpportunityHistoryRecipient",
        back_populates="event",
        cascade="all, delete-orphan",
    )


class OpportunityHistoryRecipient(Base):
    __tablename__ = "opportunity_history_recipients"
    __table_args__ = (
        UniqueConstraint(
            "history_event_id",
            "user_id",
            name="uq_opportunity_history_recipient_event_user",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    history_event_id = Column(
        Integer,
        ForeignKey("opportunity_history_events.id"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True, index=True)

    event = relationship("OpportunityHistoryEvent", back_populates="recipients")


class OpportunityBrief(Base):
    __tablename__ = "opportunity_briefs"
    __table_args__ = (UniqueConstraint("organization_id", "opportunity_id", name="uq_brief_org_opp"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)

    brief_json = Column(JSON, nullable=True)
    provider = Column(String, nullable=True)
    model = Column(String, nullable=True)
    source_basis = Column(String, nullable=True)
    sources_used = Column(JSON, nullable=True)
    filenames_processed = Column(JSON, nullable=True)
    source_summary = Column(JSON, nullable=True)

    status = Column(String, nullable=False, default="not_started", index=True)  # not_started | generating | completed | failed
    error_message = Column(Text, nullable=True)

    generated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)



class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    source = Column(String, nullable=False, default="sam.gov")
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    source_config_id = Column(Integer, ForeignKey("sam_source_configs.id"), nullable=True, index=True)
    filename = Column(String, nullable=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, nullable=False, default="completed", server_default="completed", index=True)
    retry_after_at = Column(DateTime(timezone=True), nullable=True)
    checkpoint_json = Column(JSON, nullable=True)

    processed_count = Column(Integer, nullable=False, default=0, server_default="0")
    created_count = Column(Integer, nullable=False, default=0, server_default="0")
    updated_count = Column(Integer, nullable=False, default=0, server_default="0")
    unchanged_count = Column(Integer, nullable=False, default=0, server_default="0")
    inserted_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    filtered_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)
    reason_summary_json = Column(JSON, nullable=True)

    notes = Column(Text, nullable=True)
    details = relationship(
        "IngestionRunDetail",
        back_populates="ingestion_run",
        cascade="all, delete-orphan",
    )
    update_events = relationship("OpportunityUpdateEvent", back_populates="ingestion_run")


class IngestionRunDetail(Base):
    __tablename__ = "ingestion_run_details"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    ingestion_run_id = Column(Integer, ForeignKey("ingestion_runs.id"), nullable=False, index=True)
    source = Column(String, nullable=False, index=True)
    source_record_id = Column(String, nullable=True, index=True)
    title = Column(String, nullable=True)
    result = Column(String, nullable=False, index=True)
    reason = Column(Text, nullable=False)
    matched_opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True, index=True)
    changed_fields_json = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    ingestion_run = relationship("IngestionRun", back_populates="details")


class JobRun(Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_org_started_at", "organization_id", "started_at"),
        Index("ix_job_runs_job_type_started_at", "job_type", "started_at"),
        Index("ix_job_runs_status_started_at", "status", "started_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    job_type = Column(String, nullable=False, index=True)
    trigger_type = Column(String, nullable=False, default="system", server_default="system", index=True)
    status = Column(String, nullable=False, default="running", server_default="running", index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    summary = Column(Text, nullable=True)
    details_json = Column(JSON, nullable=False, default=dict)
    error_type = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organization = relationship("Organization")


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("org_id", "opp_id", "user_id", name="uq_vote"),)

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opp_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    vote = Column(String, nullable=True, index=True)  # "PURSUE", "PASS", or null
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    ts = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    opp_id = Column(Integer, ForeignKey("opportunities.id"), nullable=True, index=True)

    event_type = Column(String, nullable=False, index=True)  # state_changed, vote_cast, opp_ingested
    ui_version = Column(String, nullable=False, default="v1")

    payload = Column(JSON, nullable=False, default=dict)

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    email_domain = Column(String, nullable=True, index=True)

    # Billing / entitlement
    plan = Column(String, default="free", nullable=False)  # free, pro, etc.
    is_active = Column(Boolean, default=True, nullable=False)
    is_live = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="organization")
    memberships = relationship("OrganizationMembership", back_populates="organization", cascade="all, delete-orphan")
    pursuit_lanes = relationship("PursuitLane", back_populates="organization", cascade="all, delete-orphan")
    workspace = relationship("Workspace", back_populates="organization", uselist=False, cascade="all, delete-orphan")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=False)
    included_user_count = Column(Integer, nullable=False, default=5, server_default="5")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspaces = relationship("Workspace", back_populates="plan")


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("organization_id", name="uq_workspace_organization"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    plan_id = Column(Integer, ForeignKey("plans.id"), nullable=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, nullable=False, index=True)
    status = Column(String, nullable=False, default="provisioned", server_default="provisioned", index=True)
    operational_contact_name = Column(String, nullable=True)
    operational_contact_email = Column(String, nullable=True)
    billing_contact_name = Column(String, nullable=True)
    billing_contact_email = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    organization = relationship("Organization", back_populates="workspace")
    plan = relationship("Plan", back_populates="workspaces")
    invitations = relationship("WorkspaceInvitation", back_populates="workspace", cascade="all, delete-orphan")
    opportunity_conversations = relationship(
        "OpportunityConversation",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    opportunity_activity_events = relationship(
        "OpportunityActivityEvent",
        back_populates="workspace",
        cascade="all, delete-orphan",
    )
    opportunity_communication_messages = relationship(
        "OpportunityCommunicationMessage",
        cascade="all, delete-orphan",
    )


class WorkspaceInvitation(Base):
    __tablename__ = "workspace_invitations"
    __table_args__ = (UniqueConstraint("token", name="uq_workspace_invitation_token"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    email = Column(String, nullable=False, index=True)
    name = Column(String, nullable=True)
    role = Column(String, nullable=False, default="admin", server_default="admin")
    token = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    organization = relationship("Organization")
    workspace = relationship("Workspace", back_populates="invitations")


class DailySnapshot(Base):
    __tablename__ = "daily_snapshots"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "snapshot_date", name="uq_daily_snapshot_workspace_user_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    status = Column(String, nullable=False, default="completed", server_default="completed", index=True)
    snapshot_json = Column(JSON, nullable=False, default=dict)

    workspace = relationship("Workspace")
    user = relationship("User")


class DailyBriefEmailDelivery(Base):
    __tablename__ = "daily_brief_email_deliveries"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "snapshot_date", name="uq_daily_brief_delivery_workspace_user_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False, index=True)
    recipient_email = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending", server_default="pending", index=True)
    attempted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    provider = Column(String, nullable=True)
    provider_message_id = Column(String, nullable=True)
    error_message = Column(Text, nullable=True)
    item_count = Column(Integer, nullable=False, default=0, server_default="0")

    organization = relationship("Organization")
    workspace = relationship("Workspace")
    user = relationship("User")


class PursuitLane(Base):
    __tablename__ = "pursuit_lanes"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    agencies = Column(JSON, nullable=False, default=list)
    naics = Column(JSON, nullable=False, default=list)
    keywords = Column(JSON, nullable=False, default=list)
    set_asides = Column(JSON, nullable=False, default=list)
    is_active = Column(Boolean, nullable=False, default=True, server_default=true())
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    organization = relationship("Organization", back_populates="pursuit_lanes")
    assignments = relationship("PursuitLaneAssignment", back_populates="pursuit_lane", cascade="all, delete-orphan")
    opportunity_matches = relationship("OpportunityPursuitLaneMatch", back_populates="pursuit_lane", cascade="all, delete-orphan")


class PursuitLaneAssignment(Base):
    __tablename__ = "pursuit_lane_assignments"
    __table_args__ = (
        UniqueConstraint("organization_id", "pursuit_lane_id", "user_id", name="uq_lane_assignment"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    pursuit_lane_id = Column(Integer, ForeignKey("pursuit_lanes.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    pursuit_lane = relationship("PursuitLane", back_populates="assignments")
    user = relationship("User")


class OpportunityPursuitLaneMatch(Base):
    __tablename__ = "opportunity_pursuit_lane_matches"
    __table_args__ = (
        UniqueConstraint("organization_id", "opportunity_id", "pursuit_lane_id", name="uq_opp_lane_match"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    pursuit_lane_id = Column(Integer, ForeignKey("pursuit_lanes.id"), nullable=False, index=True)
    matched_reasons = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    opportunity = relationship("Opportunity", back_populates="pursuit_lane_matches")
    pursuit_lane = relationship("PursuitLane", back_populates="opportunity_matches")
    
class OrgProfile(Base):
    __tablename__ = "org_profiles"
    __table_args__ = (UniqueConstraint("org_id", name="uq_org_profile"),)

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    
    sam_naics_codes = Column(Text, nullable=True)       # comma-separated, V1
    sam_days_back = Column(Integer, nullable=True)      # default fallback in code
    sam_allowed_types = Column(Text, nullable=True)     # comma-separated, V1
    include_keywords = Column(Text, nullable=True)   # comma-separated for V1
    exclude_keywords = Column(Text, nullable=True)
    include_agencies = Column(Text, nullable=True)
    exclude_agencies = Column(Text, nullable=True)

    min_days_out = Column(Integer, nullable=True)  # e.g., 3
    max_days_out = Column(Integer, nullable=True)  # e.g., 60

    digest_max_items = Column(Integer, nullable=False, default=20)
    digest_recipients = Column(Text, nullable=True)  # comma-separated emails
    digest_time_local = Column(String, nullable=True)  # "07:00" for now
    triage_enabled = Column(Boolean, nullable=False, default=False, server_default=false())
    govwin_credentials_encrypted = Column(Text, nullable=True)
    govwin_connection_status = Column(String, nullable=True)
    govwin_last_tested_at = Column(DateTime(timezone=True), nullable=True)
    govwin_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    govwin_last_sync_status = Column(String, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SalesforceConnection(Base):
    __tablename__ = "salesforce_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", name="uq_salesforce_connection_workspace"),
    )

    id = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    instance_url = Column(String, nullable=True)
    salesforce_org_id = Column(String, nullable=True)
    connected_user_id = Column(String, nullable=True)
    connected_username = Column(String, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=True)
    encrypted_access_token = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, nullable=False, default="not_connected", server_default="not_connected")
    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_connection_success_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_success_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SalesforceOAuthState(Base):
    __tablename__ = "salesforce_oauth_states"
    __table_args__ = (UniqueConstraint("state_digest", name="uq_salesforce_oauth_state_digest"),)

    id = Column(Integer, primary_key=True)
    state_digest = Column(String, nullable=False, index=True)
    encrypted_code_verifier = Column(Text, nullable=True)
    workspace_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    return_path = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ExternalIntegrationConnection(Base):
    __tablename__ = "external_integration_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "provider", name="uq_external_connection_workspace_user_provider"),
        UniqueConstraint(
            "workspace_id",
            "provider",
            "external_tenant_id",
            "external_user_id",
            name="uq_external_connection_workspace_provider_identity",
        ),
        Index("ix_external_connection_workspace_provider_status", "workspace_id", "provider", "connection_status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    provider = Column(String, nullable=False, index=True)
    connection_status = Column(String, nullable=False, default="connected", server_default="connected", index=True)
    external_tenant_id = Column(String, nullable=True, index=True)
    external_user_id = Column(String, nullable=True, index=True)
    connected_email = Column(String, nullable=True)
    connected_display_name = Column(String, nullable=True)
    encrypted_access_token = Column(Text, nullable=True)
    encrypted_refresh_token = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    granted_scopes = Column(Text, nullable=True)
    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    last_verified_at = Column(DateTime(timezone=True), nullable=True)
    last_error_at = Column(DateTime(timezone=True), nullable=True)
    last_error_code = Column(String, nullable=True)
    last_error_message = Column(Text, nullable=True)
    disconnected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    workspace = relationship("Workspace")
    user = relationship("User")


class ExternalIntegrationOAuthState(Base):
    __tablename__ = "external_integration_oauth_states"
    __table_args__ = (
        UniqueConstraint("state_digest", name="uq_external_oauth_state_digest"),
        Index("ix_external_oauth_state_provider_user_workspace", "provider", "user_id", "workspace_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)
    state_digest = Column(String, nullable=False, index=True)
    encrypted_code_verifier = Column(Text, nullable=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    return_path = Column(String, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    workspace = relationship("Workspace")
    user = relationship("User")


class SamSourceConfig(Base):
    __tablename__ = "sam_source_configs"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_sam_source_config_org_name"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String, nullable=False, default="Default SAM.gov Search")
    naics_codes = Column(JSON, nullable=False, default=list)
    keywords = Column(JSON, nullable=False, default=list)
    agencies = Column(JSON, nullable=False, default=list)
    set_asides = Column(JSON, nullable=False, default=list)
    notice_types = Column(JSON, nullable=False, default=list)
    posted_days_back = Column(Integer, nullable=False, default=30, server_default="30")
    due_days_from = Column(Integer, nullable=True)
    due_days_to = Column(Integer, nullable=True)
    active_only = Column(Boolean, nullable=False, default=True, server_default=true())
    max_records = Column(Integer, nullable=False, default=100, server_default="100")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class GrantsSourceConfig(Base):
    __tablename__ = "grants_source_configs"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_grants_source_config_org"),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true(), index=True)
    posted_days_back = Column(Integer, nullable=False, default=7, server_default="7")
    rows = Column(Integer, nullable=False, default=25, server_default="25")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    company_name = Column(String, nullable=True, index=True)
    website_url = Column(String, nullable=True)
    cage_code = Column(String, nullable=True, index=True)
    duns = Column(String, nullable=True, index=True)
    uei = Column(String, nullable=True, index=True)
    profile_json = Column(JSON, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    archived_at = Column(DateTime(timezone=True), nullable=True)

    @property
    def organization_id(self):
        return self.org_id

    @organization_id.setter
    def organization_id(self, value):
        self.org_id = value



class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)

    # Compatibility home workspace. Current workspace is resolved from memberships
    # and ?org_id in src/bidlens/tenancy.py until full auth/workspace switching exists.
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    daily_brief_email_opted_out = Column(Boolean, nullable=False, default=False, server_default=false())

    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")
    memberships = relationship("OrganizationMembership", back_populates="user", cascade="all, delete-orphan")
    user_opportunities = relationship("UserOpportunity", back_populates="user")
    opportunity_notes = relationship("OpportunityNote", back_populates="user")


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_org_membership"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String, nullable=False, default="member", server_default="member")
    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="memberships")
    user = relationship("User", back_populates="memberships")


class OpportunityNote(Base):
    __tablename__ = "opportunity_notes"

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    opportunity = relationship("Opportunity", back_populates="notes")
    user = relationship("User", back_populates="opportunity_notes")

class UserOpportunity(Base):
    __tablename__ = "user_opportunities"
    __table_args__ = (
        UniqueConstraint("user_id", "opportunity_id", name="uq_user_opportunity"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False)

    status = Column(
        Enum(
            OpportunityStatus,
            values_callable=lambda enum: [e.value for e in enum]
        ),
        default=OpportunityStatus.SAVED.value,
        nullable=False
    )
    internal_deadline = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="user_opportunities")
    opportunity = relationship("Opportunity", back_populates="user_opportunities")
    watched = Column(Boolean, nullable=False, server_default="false")

class DigestLog(Base):
    __tablename__ = "digest_log"

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

    sent_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    since_ts = Column(DateTime(timezone=True), nullable=True)
    item_count = Column(Integer, nullable=False, default=0)
