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
    __table_args__ = (UniqueConstraint("organization_id", "sam_notice_id", name="uq_opportunity_org_sam_notice"),)

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

    sam_notice_id = Column(String, nullable=False, index=True)

    title = Column(String, nullable=False)
    agency = Column(String, nullable=False)
    opportunity_type = Column(String, nullable=False)
    posted_date = Column(Date, nullable=False)
    response_deadline = Column(Date, nullable=False)
    naics = Column(String, nullable=True)
    set_aside = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    description_url = Column(Text, nullable=True)
    description_text = Column(Text, nullable=True)
    sam_url = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    upserted_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=True)

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

    user_opportunities = relationship("UserOpportunity", back_populates="opportunity")
    notes = relationship("OpportunityNote", back_populates="opportunity", cascade="all, delete-orphan")
    pursuit_lane_matches = relationship("OpportunityPursuitLaneMatch", back_populates="opportunity", cascade="all, delete-orphan")

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
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    inserted_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    filtered_count = Column(Integer, nullable=False, default=0)
    error_count = Column(Integer, nullable=False, default=0)

    notes = Column(Text, nullable=True)
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

    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="organization")
    memberships = relationship("OrganizationMembership", back_populates="organization", cascade="all, delete-orphan")
    pursuit_lanes = relationship("PursuitLane", back_populates="organization", cascade="all, delete-orphan")


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
