"""
SQLAlchemy 2.0 ORM models — UySavdo Telegram bot.

Changes v2:
  - AdMedia table: up to 10 photos + 1 video per ad (replaces single media_file_id)
  - UserAdLimit: per-user limit overrides set by admin
  - Subscription: safe upsert pattern (one active sub per user per type)
  - All queries use .scalars().first() where multiple rows possible
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    Integer, String, Text, func, UniqueConstraint, Index, SmallInteger,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────
class UserRole(str, enum.Enum):
    seller = "seller"
    buyer = "buyer"
    owner = "owner"
    seeker = "seeker"


class AdminRole(str, enum.Enum):
    super_admin = "super_admin"
    seller_admin = "seller_admin"
    buyer_admin = "buyer_admin"
    owner_admin = "owner_admin"
    seeker_admin = "seeker_admin"


class SubscriptionType(str, enum.Enum):
    standard = "standard"
    vip = "vip"
    viewer = "viewer"


class AdStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    rejected = "rejected"
    expired = "expired"
    deleted = "deleted"


class AdType(str, enum.Enum):
    sale = "sale"
    rent = "rent"


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class MediaType(str, enum.Enum):
    photo = "photo"
    video = "video"


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(256))
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    role: Mapped[Optional[UserRole]] = mapped_column(Enum(UserRole), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    ban_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subscriptions: Mapped[List["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ads: Mapped[List["Ad"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    payments: Mapped[List["Payment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ad_limit_override: Mapped[Optional["UserAdLimit"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role}>"


# ─────────────────────────────────────────────────────────────────────────────
# UserAdLimit — per-user ad limit override set by admin
# ─────────────────────────────────────────────────────────────────────────────
class UserAdLimit(Base):
    """
    Admin can grant extra ad slots to a specific user beyond their subscription
    limit.  extra_limit is ADDED on top of the subscription base limit.
    """
    __tablename__ = "user_ad_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"),
        unique=True, nullable=False
    )
    extra_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    set_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="ad_limit_override")

    def __repr__(self) -> str:
        return f"<UserAdLimit user={self.user_id} extra={self.extra_limit}>"


# ─────────────────────────────────────────────────────────────────────────────
# Admins
# ─────────────────────────────────────────────────────────────────────────────
class Admin(Base):
    __tablename__ = "admins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(256))
    role: Mapped[AdminRole] = mapped_column(Enum(AdminRole), nullable=False)
    added_by: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("admins.telegram_id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Admin id={self.telegram_id} role={self.role}>"


# ─────────────────────────────────────────────────────────────────────────────
# Blocks
# ─────────────────────────────────────────────────────────────────────────────
class Block(Base):
    __tablename__ = "blocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ads: Mapped[List["Ad"]] = relationship(back_populates="block")

    def __repr__(self) -> str:
        return f"<Block id={self.id} name={self.name}>"


# ─────────────────────────────────────────────────────────────────────────────
# Subscriptions
# NOTE: A user can have multiple historical subscription rows.
#       "Active" subscription = is_active=True AND expires_at > now.
#       Use .scalars().first() NOT .scalar_one_or_none() when querying.
# ─────────────────────────────────────────────────────────────────────────────
class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sub_type: Mapped[SubscriptionType] = mapped_column(Enum(SubscriptionType), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    payment_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    payment: Mapped[Optional["Payment"]] = relationship(
        "Payment", foreign_keys=[payment_id], back_populates="subscription"
    )

    __table_args__ = (
        Index("ix_subscriptions_user_active", "user_id", "is_active"),
        Index("ix_subscriptions_user_type_active", "user_id", "sub_type", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Subscription user={self.user_id} type={self.sub_type} active={self.is_active}>"


# ─────────────────────────────────────────────────────────────────────────────
# Payments
# ─────────────────────────────────────────────────────────────────────────────
class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    sub_type: Mapped[SubscriptionType] = mapped_column(Enum(SubscriptionType), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.pending, nullable=False
    )
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="payments")
    subscription: Mapped[Optional["Subscription"]] = relationship(
        "Subscription", foreign_keys="Subscription.payment_id", back_populates="payment"
    )

    __table_args__ = (
        UniqueConstraint("file_unique_id", name="uq_payment_file_unique_id"),
        Index("ix_payments_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Payment id={self.id} user={self.user_id} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Ads
# ─────────────────────────────────────────────────────────────────────────────
class Ad(Base):
    __tablename__ = "ads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    block_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("blocks.id", ondelete="CASCADE"), nullable=False
    )
    ad_type: Mapped[AdType] = mapped_column(Enum(AdType), nullable=False)
    sub_type: Mapped[SubscriptionType] = mapped_column(
        Enum(SubscriptionType), default=SubscriptionType.standard, nullable=False
    )
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    rooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    floor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_floors: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    area: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    status: Mapped[AdStatus] = mapped_column(
        Enum(AdStatus), default=AdStatus.pending, nullable=False
    )
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="ads")
    block: Mapped["Block"] = relationship(back_populates="ads")
    media_files: Mapped[List["AdMedia"]] = relationship(
        back_populates="ad", cascade="all, delete-orphan", order_by="AdMedia.sort_order"
    )

    __table_args__ = (
        Index("ix_ads_block_status_sub", "block_id", "status", "sub_type"),
        Index("ix_ads_owner_status", "owner_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Ad id={self.id} type={self.ad_type} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# AdMedia — up to 10 photos + 1 video per ad
# ─────────────────────────────────────────────────────────────────────────────
class AdMedia(Base):
    """
    Stores individual media files for an ad.
    - photos: up to MAX_PHOTOS (10) per ad
    - video:  up to 1 per ad, max 500 MB (checked at upload time)
    sort_order: photos first (0..9), video last (100)
    """
    __tablename__ = "ad_media"

    MAX_PHOTOS = 10
    MAX_VIDEO_SIZE = 500 * 1024 * 1024   # 500 MB in bytes
    VIDEO_SORT_ORDER = 100

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ad_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ads.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[MediaType] = mapped_column(Enum(MediaType), nullable=False)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # bytes
    sort_order: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)

    ad: Mapped["Ad"] = relationship(back_populates="media_files")

    __table_args__ = (
        Index("ix_admedia_ad_id", "ad_id"),
    )

    def __repr__(self) -> str:
        return f"<AdMedia ad={self.ad_id} type={self.media_type} order={self.sort_order}>"


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────
class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Setting key={self.key} value={self.value}>"


# ─────────────────────────────────────────────────────────────────────────────
# ThrottleLog
# ─────────────────────────────────────────────────────────────────────────────
class ThrottleLog(Base):
    __tablename__ = "throttle_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    last_request_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    banned_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_id", name="uq_throttle_user_id"),
    )
