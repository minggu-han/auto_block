import enum
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    """让 SQLAlchemy 使用 Enum.value，而不是默认的 Enum.name。

    PostgreSQL DDL 中的枚举值是大写字符串，例如 PENDING；如果使用默认 name，
    SQLAlchemy 会发送 Python 成员名 pending，导致 InvalidTextRepresentationError。
    """
    return [member.value for member in enum_cls]


class PunchType(str, enum.Enum):
    on_duty = "ON_DUTY"
    off_duty = "OFF_DUTY"


class MissingPunchStatus(str, enum.Enum):
    pending = "PENDING"
    approval_pending = "APPROVAL_PENDING"
    repaired = "REPAIRED"
    rejected = "REJECTED"


class ApprovalStatus(str, enum.Enum):
    pending = "PENDING"
    approved = "APPROVED"
    rejected = "REJECTED"


class MissingPunch(Base):
    __tablename__ = "auto_block_missing_punches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auto_block_users.id"), index=True, nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    punch_type: Mapped[PunchType] = mapped_column(
        Enum(PunchType, name="auto_block_punch_type", values_callable=enum_values),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[MissingPunchStatus] = mapped_column(
        Enum(MissingPunchStatus, name="auto_block_missing_punch_status", values_callable=enum_values),
        default=MissingPunchStatus.pending,
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    repaired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user = relationship("User", lazy="joined")


class RepairApproval(Base):
    __tablename__ = "auto_block_repair_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    missing_punch_id: Mapped[int] = mapped_column(ForeignKey("auto_block_missing_punches.id"), index=True, nullable=False)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("auto_block_users.id"), index=True, nullable=False)
    leader_id: Mapped[int] = mapped_column(ForeignKey("auto_block_users.id"), index=True, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, name="auto_block_approval_status", values_callable=enum_values),
        default=ApprovalStatus.pending,
        index=True,
        nullable=False,
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    missing_punch = relationship("MissingPunch", lazy="joined")
    applicant = relationship("User", foreign_keys=[applicant_id], lazy="joined")
    leader = relationship("User", foreign_keys=[leader_id], lazy="joined")


class RepairLog(Base):
    __tablename__ = "auto_block_repair_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    missing_punch_id: Mapped[int] = mapped_column(ForeignKey("auto_block_missing_punches.id"), index=True, nullable=False)
    operator_id: Mapped[int] = mapped_column(ForeignKey("auto_block_users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
