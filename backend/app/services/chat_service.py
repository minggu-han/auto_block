from datetime import datetime, timezone

import re

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attendance import MissingPunch, MissingPunchStatus, RepairApproval
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.schemas.schemas import ChatSessionDetailOut, ChatSessionOut


async def create_chat_session(db: AsyncSession, user: User, title: str = "补卡会话") -> ChatSession:
    session = ChatSession(user_id=user.id, title=title, current_node="start")
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def add_chat_message(db: AsyncSession, session_id: int, role: str, content: str) -> ChatMessage:
    msg = ChatMessage(session_id=session_id, role=role, content=content)
    db.add(msg)
    session = await db.get(ChatSession, session_id)
    if session:
        session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)
    return msg


async def bind_missing_punch(db: AsyncSession, session: ChatSession, missing_punch_id: int, title: str | None = None) -> ChatSession:
    session.missing_punch_id = missing_punch_id
    if title:
        session.title = title
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def update_session_node(db: AsyncSession, session: ChatSession, current_node: str) -> ChatSession:
    session.current_node = current_node
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def finish_session(db: AsyncSession, session: ChatSession, status: str = "DONE", current_node: str = "end") -> ChatSession:
    session.status = status
    session.current_node = current_node
    session.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def get_owned_session(db: AsyncSession, user: User, session_id: int) -> ChatSession | None:
    result = await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.user_id == user.id))
    return result.scalar_one_or_none()


def status_from_missing(session_status: str, missing: MissingPunch | None) -> str:
    if not missing:
        return session_status
    if missing.status == MissingPunchStatus.repaired:
        return "DONE"
    if missing.status == MissingPunchStatus.rejected:
        return "FAILED"
    if missing.status == MissingPunchStatus.approval_pending:
        return "WAITING_APPROVAL"
    return session_status


async def persist_session_status_from_missing(db: AsyncSession, session: ChatSession, missing: MissingPunch | None) -> None:
    if not missing:
        return
    display_status = status_from_missing(session.status, missing)
    changed = False
    if session.missing_punch_id != missing.id:
        session.missing_punch_id = missing.id
        changed = True
    if display_status in {"DONE", "WAITING_APPROVAL", "FAILED"} and session.status != display_status:
        session.status = display_status
        session.current_node = "end"
        changed = True
    if changed:
        session.updated_at = datetime.now(timezone.utc)
        await db.commit()


async def resolve_missing_for_session(db: AsyncSession, session: ChatSession, messages: list[ChatMessage] | None = None) -> MissingPunch | None:
    if session.missing_punch_id:
        missing = await db.get(MissingPunch, session.missing_punch_id)
        await persist_session_status_from_missing(db, session, missing)
        return missing
    if messages is None:
        messages = list((await db.execute(select(ChatMessage).where(ChatMessage.session_id == session.id))).scalars().all())
    approval_id = None
    for message in reversed(messages):
        match = re.search(r"审核单号[:：]\s*(\d+)", message.content)
        if match:
            approval_id = int(match.group(1))
            break
    if not approval_id:
        return None
    approval = await db.get(RepairApproval, approval_id)
    if not approval:
        return None
    missing = await db.get(MissingPunch, approval.missing_punch_id)
    await persist_session_status_from_missing(db, session, missing)
    return missing


async def sync_sessions_for_missing_punch(db: AsyncSession, missing_punch_id: int, status: str, approval_id: int | None = None) -> None:
    result = await db.execute(select(ChatSession).where(ChatSession.missing_punch_id == missing_punch_id))
    sessions = list(result.scalars().all())

    # ????????? /agent/chat ????????? missing_punch_id?
    # ???????????????x??????????????????
    if approval_id is not None:
        approval_pattern = f"%{approval_id}%"
        fallback = await db.execute(
            select(ChatSession)
            .join(ChatMessage, ChatMessage.session_id == ChatSession.id)
            .where(
                ChatSession.missing_punch_id.is_(None),
                ChatSession.status == "WAITING_APPROVAL",
                ChatMessage.content.like(approval_pattern),
            )
        )
        for session in fallback.scalars().all():
            session.missing_punch_id = missing_punch_id
            if session not in sessions:
                sessions.append(session)

    now = datetime.now(timezone.utc)
    for session in sessions:
        session.status = status
        session.current_node = "end"
        session.updated_at = now
    if sessions:
        await db.commit()


async def list_chat_sessions(db: AsyncSession, user: User) -> list[ChatSessionOut]:
    msg_count = select(ChatMessage.session_id, func.count(ChatMessage.id).label("cnt")).group_by(ChatMessage.session_id).subquery()
    last_msg_id = select(ChatMessage.session_id, func.max(ChatMessage.id).label("max_id")).group_by(ChatMessage.session_id).subquery()
    last_msg = select(ChatMessage.session_id, ChatMessage.content).join(last_msg_id, ChatMessage.id == last_msg_id.c.max_id).subquery()
    stmt = (
        select(ChatSession, msg_count.c.cnt, last_msg.c.content)
        .outerjoin(msg_count, ChatSession.id == msg_count.c.session_id)
        .outerjoin(last_msg, ChatSession.id == last_msg.c.session_id)
        .where(ChatSession.user_id == user.id)
        .order_by(desc(ChatSession.updated_at), desc(ChatSession.id))
    )
    rows = (await db.execute(stmt)).all()
    result_sessions = []
    for s, cnt, content in rows:
        try:
            missing = await resolve_missing_for_session(db, s)
        except Exception:
            missing = None
        display_status = status_from_missing(s.status, missing)
        display_node = "end" if display_status in {"DONE", "WAITING_APPROVAL", "FAILED"} else s.current_node
        result_sessions.append(
            ChatSessionOut(
                id=s.id,
                user_id=s.user_id,
                missing_punch_id=missing.id if missing else s.missing_punch_id,
                repair_work_date=missing.work_date if missing else None,
                repair_punch_type=missing.punch_type if missing else None,
                repair_status=missing.status if missing else None,
                title=s.title,
                status=display_status,
                current_node=display_node,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=cnt or 0,
                last_message=content,
            )
        )
    return result_sessions


async def get_chat_session_detail(db: AsyncSession, user: User, session_id: int) -> ChatSessionDetailOut | None:
    session = await get_owned_session(db, user, session_id)
    if not session:
        return None
    messages = list((await db.execute(select(ChatMessage).where(ChatMessage.session_id == session.id).order_by(ChatMessage.id.asc()))).scalars().all())
    missing = await resolve_missing_for_session(db, session, messages)
    return ChatSessionDetailOut(
        id=session.id,
        user_id=session.user_id,
        missing_punch_id=missing.id if missing else session.missing_punch_id,
        repair_work_date=missing.work_date if missing else None,
        repair_punch_type=missing.punch_type if missing else None,
        repair_status=missing.status if missing else None,
        title=session.title,
        status=status_from_missing(session.status, missing),
        current_node="end" if status_from_missing(session.status, missing) in {"DONE", "WAITING_APPROVAL", "FAILED"} else session.current_node,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=messages,
    )

