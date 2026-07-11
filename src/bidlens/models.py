from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
import enum
from .database import Base
from sqlalchemy import UniqueConstraint
from sqlalchemy import JSON, func
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
    crm_pushed = Column(Boolean, nullable=False, default=False, server_default="0", index=True)
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
    is_active = Column(Boolean, nullable=False, default=True, server_default="1")
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
    triage_enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    govwin_credentials_encrypted = Column(Text, nullable=True)
    govwin_connection_status = Column(String, nullable=True)
    govwin_last_tested_at = Column(DateTime(timezone=True), nullable=True)
    govwin_last_sync_at = Column(DateTime(timezone=True), nullable=True)
    govwin_last_sync_status = Column(String, nullable=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


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
    active_only = Column(Boolean, nullable=False, default=True, server_default="1")
    max_records = Column(Integer, nullable=False, default=100, server_default="100")
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
