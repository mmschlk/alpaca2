import json
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SuggestionType(str, PyEnum):
    conference = "conference"
    conference_edition = "conference_edition"
    journal = "journal"
    journal_special_issue = "journal_special_issue"


class SuggestionStatus(str, PyEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class Suggestion(Base):
    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    entity_type: Mapped[SuggestionType] = mapped_column(Enum(SuggestionType), nullable=False)
    status: Mapped[SuggestionStatus] = mapped_column(
        Enum(SuggestionStatus), default=SuggestionStatus.pending, nullable=False, index=True
    )
    data: Mapped[str] = mapped_column(Text, nullable=False)  # JSON payload
    submitted_by_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewed_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    submitted_by: Mapped["User"] = relationship("User", foreign_keys=[submitted_by_id])
    reviewed_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[reviewed_by_id])

    @property
    def data_dict(self) -> dict:
        return json.loads(self.data)

    @property
    def type_label(self) -> str:
        return {
            SuggestionType.conference: "Conference",
            SuggestionType.conference_edition: "Conference Edition",
            SuggestionType.journal: "Journal",
            SuggestionType.journal_special_issue: "Journal Special Issue",
        }[self.entity_type]
