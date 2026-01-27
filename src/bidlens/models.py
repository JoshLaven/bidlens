from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
import enum
from .database import Base
from sqlalchemy import UniqueConstraint
from sqlalchemy import JSON, func
from pydantic import BaseModel
from typing import Optional

class OpportunityStatus(str, enum.Enum):
    SAVED = "saved"
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"

class Opportunity(Base):
    __tablename__ = "opportunities"
    
    id = Column(Integer, primary_key=True, index=True)
    sam_notice_id = Column(String, unique=True, nullable=False, index=True)
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

