from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
import enum
from .database import Base
from sqlalchemy import UniqueConstraint
from sqlalchemy import JSON, func
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import Boolean

class OpportunityStatus(str, enum.Enum):
    SAVED = "saved"
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"

class Opportunity(Base):
    __tablename__ = "opportunities"
    
    id = Column(Integer, primary_key=True, index=True)
    sam_notice_id = Column(String, unique=True, nullable=False, index=True)
    #organization_name=Column(String, nullable=True)
    title = Column(String, nullable=False)
    agency = Column(String, nullable=False)
    opportunity_type = Column(String, nullable=False)
    posted_date = Column(Date, nullable=False)
    response_deadline = Column(Date, nullable=False)
    naics = Column(String, nullable=True)
    set_aside = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    sam_url = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user_opportunities = relationship("UserOpportunity", back_populates="opportunity")
    # store original record for future enrichment without re-pulling
    #raw_json = Column(Text, nullable=True)

#Index("ix_opps_notice_id", Opportunity.notice_id)

class OpportunityBrief(Base):
    __tablename__ = "opportunity_briefs"
    __table_args__ = (UniqueConstraint("opportunity_id", name="uq_brief_opp"),)

    id = Column(Integer, primary_key=True, index=True)
    opportunity_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)

    brief_json = Column(JSON, nullable=True)
    model = Column(String, nullable=True)

    status = Column(String, nullable=False, default="pending", index=True)  # pending | ok | failed
    error_message = Column(Text, nullable=True)

    generated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)



class OpportunityState(Base):
    __tablename__ = "opportunity_states"
    __table_args__ = (UniqueConstraint("org_id", "opp_id", name="uq_org_opp_state"),)

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opp_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)

    state = Column(String, nullable=False, default="FEED", index=True)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("org_id", "opp_id", "user_id", name="uq_vote"),)

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    opp_id = Column(Integer, ForeignKey("opportunities.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    vote = Column(String, nullable=True, index=True)  # "UP", "DOWN", or null
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class VoteIn(BaseModel):
    opp_id: int
    vote: Optional[str] = None  # "UP", "DOWN", or null
    ui_version: str

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

    # Billing / entitlement
    plan = Column(String, default="free", nullable=False)  # free, pro, etc.
    is_active = Column(Boolean, default=True, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="organization")
    
class OrgProfile(Base):
    __tablename__ = "org_profiles"
    __table_args__ = (UniqueConstraint("org_id", name="uq_org_profile"),)

    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)

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



class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)

    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    organization = relationship("Organization", back_populates="users")
    user_opportunities = relationship("UserOpportunity", back_populates="user")

class UserOpportunity(Base):
    __tablename__ = "user_opportunities"
    __table_args__ = (
        UniqueConstraint("user_id", "opportunity_id", name="uq_user_opportunity"),
    )

    id = Column(Integer, primary_key=True, index=True)
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
