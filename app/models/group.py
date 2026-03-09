from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class GroupRole(str, PyEnum):
    admin = "admin"
    member = "member"


class GroupReviewRequestStatus(str, PyEnum):
    open = "open"
    assigned = "assigned"
    completed = "completed"
    cancelled = "cancelled"


class ResearchGroup(Base):
    __tablename__ = "research_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    logo_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    parent_group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    parent: Mapped[Optional["ResearchGroup"]] = relationship(
        "ResearchGroup", remote_side="ResearchGroup.id", back_populates="subgroups"
    )
    subgroups: Mapped[list["ResearchGroup"]] = relationship("ResearchGroup", back_populates="parent")
    memberships: Mapped[list["GroupMembership"]] = relationship(
        "GroupMembership", back_populates="group", cascade="all, delete-orphan"
    )
    paper_shares: Mapped[list["PaperGroupShare"]] = relationship("PaperGroupShare", back_populates="group")
    review_balances: Mapped[list["GroupReviewBalance"]] = relationship(
        "GroupReviewBalance", back_populates="group", cascade="all, delete-orphan"
    )
    review_requests: Mapped[list["GroupReviewRequest"]] = relationship(
        "GroupReviewRequest", back_populates="group", cascade="all, delete-orphan"
    )


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[GroupRole] = mapped_column(Enum(GroupRole), default=GroupRole.member, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped["ResearchGroup"] = relationship("ResearchGroup", back_populates="memberships")
    user: Mapped["User"] = relationship("User", back_populates="group_memberships")


class GroupReviewBalance(Base):
    __tablename__ = "group_review_balances"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_review_balance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    group: Mapped["ResearchGroup"] = relationship("ResearchGroup", back_populates="review_balances")
    user: Mapped["User"] = relationship("User", back_populates="review_balances")


class GroupReviewRequest(Base):
    __tablename__ = "group_review_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False, index=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    paper_id: Mapped[Optional[int]] = mapped_column(ForeignKey("paper_projects.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[GroupReviewRequestStatus] = mapped_column(
        Enum(GroupReviewRequestStatus), default=GroupReviewRequestStatus.open, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    group: Mapped["ResearchGroup"] = relationship("ResearchGroup", back_populates="review_requests")
    requester: Mapped["User"] = relationship("User", foreign_keys=[requester_id], back_populates="review_requests_made")
    paper: Mapped[Optional["PaperProject"]] = relationship("PaperProject")
    assignment: Mapped[Optional["GroupReviewAssignment"]] = relationship(
        "GroupReviewAssignment", back_populates="request", uselist=False, cascade="all, delete-orphan"
    )


class GroupReviewAssignment(Base):
    __tablename__ = "group_review_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("group_review_requests.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    request: Mapped["GroupReviewRequest"] = relationship("GroupReviewRequest", back_populates="assignment")
    reviewer: Mapped["User"] = relationship("User", foreign_keys=[reviewer_id], back_populates="review_assignments")
