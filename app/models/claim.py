from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ClaimStatus(str, PyEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class AuthorClaimRequest(Base):
    __tablename__ = "author_claim_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[ClaimStatus] = mapped_column(
        Enum(ClaimStatus), default=ClaimStatus.pending, nullable=False
    )
    message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    reviewed_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id], back_populates="claim_requests")
    author: Mapped["Author"] = relationship("Author", back_populates="claim_requests")
    reviewer: Mapped[Optional["User"]] = relationship("User", foreign_keys=[reviewed_by])
