from datetime import datetime, date
from sqlalchemy import Column, Integer, String, Boolean, Date, DateTime, Text, ForeignKey, Enum
from sqlalchemy.orm import relationship
import enum
from .database import Base
from sqlalchemy import UniqueConstraint

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

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    is_paid = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
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
        Enum(OpportunityStatus),
        default=OpportunityStatus.SAVED,
        nullable=False
    )

    internal_deadline = Column(Date, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="user_opportunities")
    opportunity = relationship("Opportunity", back_populates="user_opportunities")

