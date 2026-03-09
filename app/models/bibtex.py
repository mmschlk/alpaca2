from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class BibCollection(Base):
    __tablename__ = "bib_collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Group-owned collections: owned by a research group, auto-shared with members
    group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    style: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    owner: Mapped[Optional["User"]] = relationship("User", foreign_keys=[owner_id])
    group: Mapped[Optional["ResearchGroup"]] = relationship("ResearchGroup", foreign_keys=[group_id])
    entries: Mapped[list["BibEntry"]] = relationship(
        "BibEntry", back_populates="collection", cascade="all, delete-orphan",
        order_by="BibEntry.position",
    )
    shares: Mapped[list["BibCollectionShare"]] = relationship(
        "BibCollectionShare", back_populates="collection", cascade="all, delete-orphan",
    )
    write_revokes: Mapped[list["BibCollectionWriteRevoke"]] = relationship(
        "BibCollectionWriteRevoke", back_populates="collection", cascade="all, delete-orphan",
    )


class BibCollectionShare(Base):
    __tablename__ = "bib_collection_shares"
    __table_args__ = (UniqueConstraint("collection_id", "group_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("bib_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("research_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )

    collection: Mapped["BibCollection"] = relationship("BibCollection", back_populates="shares")
    group: Mapped["ResearchGroup"] = relationship("ResearchGroup")


class BibCollectionWriteRevoke(Base):
    """Tracks users whose write access to a group-owned collection has been revoked."""
    __tablename__ = "bib_collection_write_revokes"
    __table_args__ = (UniqueConstraint("collection_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("bib_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    collection: Mapped["BibCollection"] = relationship("BibCollection", back_populates="write_revokes")
    user: Mapped["User"] = relationship("User")


class BibEntry(Base):
    __tablename__ = "bib_entries"
    __table_args__ = (UniqueConstraint("collection_id", "cite_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("bib_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entry_type: Mapped[str] = mapped_column(String(32), nullable=False)   # article, inproceedings, ...
    cite_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    authors_raw: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fields_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)  # remaining fields
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    collection: Mapped["BibCollection"] = relationship("BibCollection", back_populates="entries")
