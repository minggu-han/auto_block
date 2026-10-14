from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ChatSession(Base):
    __tablename__ = "auto_block_chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("auto_block_users.id"), index=True, nullable=False)
    missing_punch_id: Mapped[int | None] = mapped_column(ForeignKey("auto_block_missing_punches.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="补卡会话")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    current_node: Mapped[str] = mapped_column(String(64), nullable=False, default="start")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan", lazy="selectin")


class ChatMessage(Base):
    __tablename__ = "auto_block_chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("auto_block_chat_sessions.id"), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session = relationship("ChatSession", back_populates="messages")

