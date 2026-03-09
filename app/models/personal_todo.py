from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.paper import TodoStatus


class PersonalTodo(Base):
    __tablename__ = "personal_todos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[TodoStatus] = mapped_column(
        Enum(TodoStatus), default=TodoStatus.open, nullable=False
    )
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    source_workflow_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User", back_populates="personal_todos")
    source_workflow: Mapped[Optional["Workflow"]] = relationship(
        "Workflow", foreign_keys=[source_workflow_id]
    )
