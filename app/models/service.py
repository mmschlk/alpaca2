from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, SmallInteger, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ServiceRole(str, PyEnum):
    reviewer = "reviewer"
    area_chair = "area_chair"
    senior_program_committee = "senior_program_committee"
    program_chair = "program_chair"
    workshop_organizer = "workshop_organizer"
    associate_editor = "associate_editor"
    editor_in_chief = "editor_in_chief"
    editorial_board = "editorial_board"
    other = "other"


SERVICE_ROLE_LABELS = {
    ServiceRole.reviewer: "Reviewer",
    ServiceRole.area_chair: "Area Chair",
    ServiceRole.senior_program_committee: "Senior PC",
    ServiceRole.program_chair: "Program Chair",
    ServiceRole.workshop_organizer: "Workshop Organizer",
    ServiceRole.associate_editor: "Associate Editor",
    ServiceRole.editor_in_chief: "Editor in Chief",
    ServiceRole.editorial_board: "Editorial Board",
    ServiceRole.other: "Other",
}

SERVICE_ROLE_COLORS = {
    ServiceRole.reviewer: "secondary",
    ServiceRole.area_chair: "primary",
    ServiceRole.senior_program_committee: "info",
    ServiceRole.program_chair: "danger",
    ServiceRole.workshop_organizer: "warning",
    ServiceRole.associate_editor: "success",
    ServiceRole.editor_in_chief: "dark",
    ServiceRole.editorial_board: "primary",
    ServiceRole.other: "secondary",
}


class ServiceRecord(Base):
    __tablename__ = "service_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Exactly one of these should be set
    conference_edition_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("conference_editions.id", ondelete="SET NULL"), nullable=True
    )
    journal_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("journals.id", ondelete="SET NULL"), nullable=True
    )
    year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    role: Mapped[ServiceRole] = mapped_column(Enum(ServiceRole), nullable=False)
    num_papers: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship("User", back_populates="service_records")
    conference_edition: Mapped[Optional["ConferenceEdition"]] = relationship(
        "ConferenceEdition", lazy="joined"
    )
    journal: Mapped[Optional["Journal"]] = relationship("Journal", lazy="joined")

    @property
    def venue_label(self) -> str:
        if self.conference_edition and self.conference_edition.conference:
            c = self.conference_edition.conference
            return f"{c.abbreviation} {self.conference_edition.year}"
        if self.journal:
            return f"{self.journal.abbreviation or self.journal.name} {self.year}"
        return f"Unknown {self.year}"
