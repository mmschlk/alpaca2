from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(String(512), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    user_overrides: Mapped[list["UserFeatureAccess"]] = relationship(
        "UserFeatureAccess", back_populates="feature", cascade="all, delete-orphan"
    )


class UserFeatureAccess(Base):
    __tablename__ = "user_feature_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    feature_key: Mapped[str] = mapped_column(
        String(64), ForeignKey("feature_flags.key", ondelete="CASCADE"), primary_key=True
    )

    user: Mapped["User"] = relationship("User")
    feature: Mapped["FeatureFlag"] = relationship("FeatureFlag", back_populates="user_overrides")
