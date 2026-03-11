from datetime import date, datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    Date, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.paper import TodoStatus  # reuse open/in_progress/done


class SupervisionProjectType(str, PyEnum):
    masters_thesis   = "masters_thesis"
    bachelors_thesis = "bachelors_thesis"
    master_project   = "master_project"
    seminar          = "seminar"


class SupervisionStatus(str, PyEnum):
    incubating = "incubating"
    ongoing    = "ongoing"
    submitted  = "submitted"
    archived   = "archived"


class SupervisionDocumentType(str, PyEnum):
    expose       = "expose"
    registration = "registration"
    draft        = "draft"
    final        = "final"
    other        = "other"


# ── human-readable labels used in templates ────────────────────────────────
PROJECT_TYPE_LABELS = {
    SupervisionProjectType.masters_thesis:   "Master's Thesis",
    SupervisionProjectType.bachelors_thesis: "Bachelor's Thesis",
    SupervisionProjectType.master_project:   "Master Project",
    SupervisionProjectType.seminar:          "Seminar",
}

DOCUMENT_TYPE_LABELS = {
    SupervisionDocumentType.expose:       "Exposé",
    SupervisionDocumentType.registration: "Registration",
    SupervisionDocumentType.draft:        "Draft",
    SupervisionDocumentType.final:        "Final Thesis",
    SupervisionDocumentType.other:        "Other",
}


class SupervisionProject(Base):
    __tablename__ = "supervision_projects"

    id:              Mapped[int]          = mapped_column(Integer, primary_key=True, index=True)
    supervisor_id:   Mapped[int]          = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title:           Mapped[str]          = mapped_column(String(512), nullable=False)
    student_name:    Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    student_email:   Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    project_type:    Mapped[SupervisionProjectType] = mapped_column(
        Enum(SupervisionProjectType), nullable=False
    )
    status:          Mapped[SupervisionStatus] = mapped_column(
        Enum(SupervisionStatus), nullable=False, default=SupervisionStatus.incubating
    )
    start_date:      Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    end_date:        Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    github_url:      Mapped[Optional[str]]  = mapped_column(String(512), nullable=True)
    notes:           Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime, server_default=func.now())
    updated_at:      Mapped[datetime]       = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    supervisor: Mapped["User"] = relationship("User", foreign_keys=[supervisor_id])  # type: ignore[name-defined]
    documents:  Mapped[list["SupervisionDocument"]] = relationship(
        "SupervisionDocument", back_populates="project",
        cascade="all, delete-orphan", order_by="SupervisionDocument.uploaded_at",
    )
    todos:      Mapped[list["SupervisionTodo"]] = relationship(
        "SupervisionTodo", back_populates="project",
        cascade="all, delete-orphan", order_by="SupervisionTodo.position",
    )


class SupervisionDocument(Base):
    __tablename__ = "supervision_documents"

    id:            Mapped[int]          = mapped_column(Integer, primary_key=True, index=True)
    project_id:    Mapped[int]          = mapped_column(
        ForeignKey("supervision_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label:         Mapped[str]          = mapped_column(String(255), nullable=False)
    document_type: Mapped[SupervisionDocumentType] = mapped_column(
        Enum(SupervisionDocumentType), nullable=False, default=SupervisionDocumentType.other
    )
    file_path:     Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    url:           Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    uploaded_by:   Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    uploaded_at:   Mapped[datetime]      = mapped_column(DateTime, server_default=func.now())

    project: Mapped["SupervisionProject"] = relationship("SupervisionProject", back_populates="documents")


class SupervisionTodo(Base):
    __tablename__ = "supervision_todos"

    id:                      Mapped[int]             = mapped_column(Integer, primary_key=True, index=True)
    project_id:              Mapped[int]             = mapped_column(
        ForeignKey("supervision_projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title:                   Mapped[str]             = mapped_column(String(512), nullable=False)
    description:             Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
    status:                  Mapped[TodoStatus]       = mapped_column(
        Enum(TodoStatus), nullable=False, default=TodoStatus.open
    )
    due_date:                Mapped[Optional[date]]  = mapped_column(Date, nullable=True)
    position:                Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    source_workflow_step_id: Mapped[Optional[int]]   = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="SET NULL"), nullable=True
    )
    created_at:              Mapped[datetime]        = mapped_column(DateTime, server_default=func.now())
    updated_at:              Mapped[datetime]        = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    project:             Mapped["SupervisionProject"] = relationship("SupervisionProject", back_populates="todos")
    source_workflow_step: Mapped[Optional["WorkflowStep"]] = relationship(  # type: ignore[name-defined]
        "WorkflowStep", foreign_keys=[source_workflow_step_id]
    )


class SupervisionTypeWorkflowConfig(Base):
    __tablename__ = "supervision_type_workflow_configs"
    __table_args__ = (UniqueConstraint("user_id", "project_type"),)

    id:           Mapped[int]                    = mapped_column(Integer, primary_key=True, index=True)
    user_id:      Mapped[int]                    = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_type: Mapped[SupervisionProjectType] = mapped_column(
        Enum(SupervisionProjectType), nullable=False
    )
    workflow_id:  Mapped[Optional[int]]          = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )

    workflow: Mapped[Optional["Workflow"]] = relationship("Workflow", foreign_keys=[workflow_id])  # type: ignore[name-defined]
