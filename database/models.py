"""
SQLAlchemy 2.0 ORM models for UySavdo Telegram bot.
All queries must go through ORM — no raw SQL strings anywhere.
"""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Optional, List

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey,
    Integer, String, Text, func, UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ─────────────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────
class UserRole(str, enum.Enum):
    seller = "seller"        # Uy sotuvchi
    buyer = "buyer"          # Uy sotib oluvchi
    owner = "owner"          # Kvartira ijaraga beruvchi
    seeker = "seeker"        # Kvartira ijaraga oluvchi


class AdminRole(str, enum.Enum):
    super_admin = "super_admin"
    seller_admin = "seller_admin"
    buyer_admin = "buyer_admin"
    owner_admin = "owner_admin"
    seeker_admin = "seeker_admin"


class SubscriptionType(str, enum.Enum):
    standard = "standard"
    vip = "vip"
    viewer = "viewer"   # for buyer / seeker


class AdStatus(str, enum.Enum):
    pending = "pending"         # waiting for admin approval
    active = "active"
    rejected = "rejected"
    expired = "expired"
    deleted = "deleted"


class AdType(str, enum.Enum):
    sale = "sale"        # Sotish (Seller)
    rent = "rent"        # Ijaraga berish (Owner)


class PaymentStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)           # Telegram user_id
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

    # Relationships
    subscriptions: Mapped[List["Subscription"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    ads: Mapped[List["Ad"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    payments: Mapped[List["Payment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} role={self.role}>"


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
# Blocks (Domlar / Mahallalar)
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
    # Chek fraud protection: file_unique_id must be unique in DB
    file_unique_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_id: Mapped[str] = mapped_column(String(256), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)            # UZS
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.pending, nullable=False
    )
    reviewed_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)   # admin telegram_id
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
# Ads (E'lonlar)
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
    price: Mapped[int] = mapped_column(Integer, nullable=False)            # UZS
    rooms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    floor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_floors: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    area: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)   # m²
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # Media: stored as Telegram file_id (photo or video)
    media_file_id: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # "photo" | "video"
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

    __table_args__ = (
        Index("ix_ads_block_status_sub", "block_id", "status", "sub_type"),
        Index("ix_ads_owner_status", "owner_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<Ad id={self.id} type={self.ad_type} status={self.status}>"


# ─────────────────────────────────────────────────────────────────────────────
# Settings (Admin tomonidan o'zgartiriladi)
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
# Throttle Log (Anti-flood tracking)
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
