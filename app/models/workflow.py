from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkflowTriggerType(str, PyEnum):
    paper_status = "paper_status"   # fires when paper.status → target_status
    group_join   = "group_join"     # fires when user joins group_id


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    owner: Mapped[Optional["User"]] = relationship("User", back_populates="workflows_owned")
    steps: Mapped[list["WorkflowStep"]] = relationship(
        "WorkflowStep", back_populates="workflow",
        cascade="all, delete-orphan", order_by="WorkflowStep.position"
    )
    triggers: Mapped[list["WorkflowTrigger"]] = relationship(
        "WorkflowTrigger", back_populates="workflow", cascade="all, delete-orphan"
    )
    shares: Mapped[list["WorkflowShare"]] = relationship(
        "WorkflowShare", back_populates="workflow", cascade="all, delete-orphan"
    )
    paper_subscriptions: Mapped[list["PaperWorkflowSubscription"]] = relationship(
        "PaperWorkflowSubscription", back_populates="workflow", cascade="all, delete-orphan"
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    due_offset_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    depends_on_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="SET NULL"), nullable=True
    )

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="steps")
    depends_on: Mapped[Optional["WorkflowStep"]] = relationship(
        "WorkflowStep", foreign_keys="[WorkflowStep.depends_on_id]",
        remote_side="[WorkflowStep.id]", uselist=False
    )


class WorkflowShare(Base):
    __tablename__ = "workflow_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_with_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    shared_with_group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=True, index=True
    )

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="shares")
    shared_with_user: Mapped[Optional["User"]] = relationship(
        "User", foreign_keys=[shared_with_user_id]
    )
    shared_with_group: Mapped[Optional["ResearchGroup"]] = relationship(
        "ResearchGroup", foreign_keys=[shared_with_group_id]
    )


class WorkflowTrigger(Base):
    __tablename__ = "workflow_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger_type: Mapped[WorkflowTriggerType] = mapped_column(
        Enum(WorkflowTriggerType), nullable=False
    )
    target_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )

    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="triggers")
    group: Mapped[Optional["ResearchGroup"]] = relationship(
        "ResearchGroup", foreign_keys=[group_id]
    )


class PaperWorkflowSubscription(Base):
    """Subscribe a specific paper to a workflow.
    When the paper changes to a status matching any of the workflow's
    paper_status triggers, that workflow fires only for this paper."""

    __tablename__ = "paper_workflow_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    paper_id: Mapped[int] = mapped_column(
        ForeignKey("paper_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )

    paper: Mapped["PaperProject"] = relationship("PaperProject", back_populates="workflow_subscriptions")
    workflow: Mapped["Workflow"] = relationship("Workflow", back_populates="paper_subscriptions")
