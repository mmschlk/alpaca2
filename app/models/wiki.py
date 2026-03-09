from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

LOCK_TTL = 600  # seconds — edit lock time-to-live


class WikiPage(Base):
    __tablename__ = "wiki_pages"
    __table_args__ = (UniqueConstraint("group_id", "slug", name="uq_wiki_page_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    locked_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    group: Mapped["ResearchGroup"] = relationship("ResearchGroup")
    created_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[created_by_id])
    locked_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[locked_by_id])
    revisions: Mapped[list["WikiPageRevision"]] = relationship(
        "WikiPageRevision", back_populates="page",
        cascade="all, delete-orphan", order_by="WikiPageRevision.edited_at.desc()"
    )

    @property
    def is_locked(self) -> bool:
        if self.locked_by_id is None or self.locked_at is None:
            return False
        age = (datetime.now(timezone.utc) - self.locked_at.replace(tzinfo=timezone.utc)).total_seconds()
        return age < LOCK_TTL

    def locked_by_other(self, user_id: int) -> bool:
        return self.is_locked and self.locked_by_id != user_id

    @property
    def lock_minutes_ago(self) -> int:
        if self.locked_at is None:
            return 0
        delta = datetime.now(timezone.utc) - self.locked_at.replace(tzinfo=timezone.utc)
        return int(delta.total_seconds() // 60)


class WikiPageRevision(Base):
    __tablename__ = "wiki_page_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    page_id: Mapped[int] = mapped_column(
        ForeignKey("wiki_pages.id", ondelete="CASCADE"), nullable=False, index=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    edited_by_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    edited_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    edit_note: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    page: Mapped["WikiPage"] = relationship("WikiPage", back_populates="revisions")
    edited_by: Mapped[Optional["User"]] = relationship("User", foreign_keys=[edited_by_id])
