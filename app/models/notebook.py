from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class NotebookEntry(Base):
    __tablename__ = "notebook_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    paper_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("paper_projects.id", ondelete="SET NULL"), nullable=True
    )
    conference_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("conferences.id", ondelete="SET NULL"), nullable=True
    )
    map_x: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    map_y: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])
    paper: Mapped[Optional["PaperProject"]] = relationship("PaperProject", foreign_keys=[paper_id])
    conference: Mapped[Optional["Conference"]] = relationship("Conference", foreign_keys=[conference_id])

    entry_tags: Mapped[list["NotebookEntryTag"]] = relationship(
        "NotebookEntryTag", back_populates="entry", cascade="all, delete-orphan"
    )
    shared_groups: Mapped[list["NotebookEntryShare"]] = relationship(
        "NotebookEntryShare", back_populates="entry", cascade="all, delete-orphan"
    )
    edges_out: Mapped[list["NotebookEdge"]] = relationship(
        "NotebookEdge", foreign_keys="NotebookEdge.source_id",
        back_populates="source", cascade="all, delete-orphan"
    )
    edges_in: Mapped[list["NotebookEdge"]] = relationship(
        "NotebookEdge", foreign_keys="NotebookEdge.target_id",
        back_populates="target", cascade="all, delete-orphan"
    )


class NotebookTag(Base):
    __tablename__ = "notebook_tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_notebook_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)

    user: Mapped["User"] = relationship("User")
    entry_tags: Mapped[list["NotebookEntryTag"]] = relationship(
        "NotebookEntryTag", back_populates="tag", cascade="all, delete-orphan"
    )


class NotebookEntryTag(Base):
    __tablename__ = "notebook_entry_tags"

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("notebook_entries.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("notebook_tags.id", ondelete="CASCADE"), primary_key=True
    )

    entry: Mapped["NotebookEntry"] = relationship("NotebookEntry", back_populates="entry_tags")
    tag: Mapped["NotebookTag"] = relationship("NotebookTag", back_populates="entry_tags")


class NotebookEntryShare(Base):
    __tablename__ = "notebook_entry_shares"

    entry_id: Mapped[int] = mapped_column(
        ForeignKey("notebook_entries.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("research_groups.id", ondelete="CASCADE"), primary_key=True
    )

    entry: Mapped["NotebookEntry"] = relationship("NotebookEntry", back_populates="shared_groups")
    group: Mapped["ResearchGroup"] = relationship("ResearchGroup")


class NotebookEdge(Base):
    __tablename__ = "notebook_edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("notebook_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("notebook_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    source: Mapped["NotebookEntry"] = relationship(
        "NotebookEntry", foreign_keys=[source_id], back_populates="edges_out"
    )
    target: Mapped["NotebookEntry"] = relationship(
        "NotebookEntry", foreign_keys=[target_id], back_populates="edges_in"
    )
