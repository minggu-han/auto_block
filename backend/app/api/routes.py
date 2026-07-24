import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.attendance import ApprovalStatus, MissingPunch, RepairApproval
from app.models.user import User
from app.schemas.schemas import (
    ApprovalOut,
    ApprovalReviewRequest,
    AgentChatRequest,
    ChatSelectRequest,
    ChatSessionDetailOut,
    ChatSessionOut,
    LoginRequest,
    MissingPunchCreate,
    MissingPunchOut,
    Token,
    UserOut,
)
from app.services.agent import AutoClockAgent
from app.services.attendance_service import create_missing_punch, list_leader_approvals, list_my_missing_punches, review_approval
from app.services.auth_service import authenticate_user
from app.services.chat_service import add_chat_message, bind_missing_punch, create_chat_session, finish_session, get_chat_session_detail, get_owned_session, list_chat_sessions, sync_sessions_for_missing_punch, update_session_node

router = APIRouter(prefix="/api")


def sse(data: dict | str, event: str = "message") -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def repair_record_payload(missing: MissingPunch | None) -> dict:
    if not missing:
        return {"repair_record": None}
    punch_label = "\u4e0a\u73ed\u5361" if missing.punch_type.value == "ON_DUTY" else "\u4e0b\u73ed\u5361"
    return {
        "repair_record": {
            "id": missing.id,
            "work_date": missing.work_date.isoformat(),
            "punch_type": missing.punch_type.value,
            "punch_type_label": punch_label,
            "status": missing.status.value,
        }
    }


def repair_result_payload(status: str, current_node: str, terminal: bool, missing: MissingPunch | None = None) -> dict:
    payload = repair_record_payload(missing)
    if not terminal:
        payload.update({"result_status": "IN_PROGRESS", "result_message": "\u672c\u8f6e\u5bf9\u8bdd\u5c1a\u672a\u7ed3\u675f\uff0c\u8bf7\u7ee7\u7eed\u6309\u63d0\u793a\u64cd\u4f5c\u3002"})
    elif status == "DONE":
        payload.update({"result_status": "SUCCESS", "result_message": "\u8865\u5361\u6210\u529f\uff0c\u5f53\u524d\u8bb0\u5f55\u5df2\u81ea\u52a8\u8865\u5361\u5b8c\u6210\u3002"})
    elif status == "WAITING_APPROVAL":
        payload.update({"result_status": "PENDING_APPROVAL", "result_message": "\u5df2\u63d0\u4ea4\u9886\u5bfc\u5ba1\u6838\uff0c\u9886\u5bfc\u901a\u8fc7\u540e\u4f1a\u81ea\u52a8\u8865\u5361\u3002"})
    elif status == "FAILED":
        payload.update({"result_status": "FAILED", "result_message": "\u8865\u5361\u5931\u8d25\uff0c\u8bf7\u67e5\u770b\u804a\u5929\u5185\u5bb9\u4e2d\u7684\u5931\u8d25\u539f\u56e0\u3002"})
    else:
        payload.update({"result_status": "FAILED", "result_message": "\u672c\u8f6e\u8865\u5361\u6d41\u7a0b\u5df2\u7ed3\u675f\uff0c\u4f46\u6ca1\u6709\u5b8c\u6210\u8865\u5361\uff0c\u8bf7\u67e5\u770b\u804a\u5929\u5185\u5bb9\u786e\u8ba4\u539f\u56e0\u3002"})
    return payload



@router.post("/auth/login", response_model=Token)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, data.username, data.password)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return Token(access_token=create_access_token(user.username))


@router.get("/users/me", response_model=UserOut)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/missing-punches", response_model=MissingPunchOut)
async def add_missing_punch(data: MissingPunchCreate, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await create_missing_punch(db, current_user, data)


@router.get("/missing-punches", response_model=list[MissingPunchOut])
async def my_missing_punches(only_open: bool = False, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await list_my_missing_punches(db, current_user, only_open)


@router.get("/chat-sessions", response_model=list[ChatSessionOut])
async def chat_sessions(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await list_chat_sessions(db, current_user)


@router.get("/chat-sessions/{session_id}", response_model=ChatSessionDetailOut)
async def chat_session_detail(session_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    detail = await get_chat_session_detail(db, current_user, session_id)
    if not detail:
        raise HTTPException(status_code=404, detail="聊天记录不存在")
    return detail



@router.post("/agent/chat")
async def agent_chat(data: AgentChatRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    async def gen() -> AsyncIterator[str]:
        chat = await get_owned_session(db, current_user, data.session_id) if data.session_id else None
        if not chat:
            title = data.message[:40] if data.message else "补卡会话"
            chat = await create_chat_session(db, current_user, title=title or "补卡会话")
            yield sse({"type": "session", "session_id": chat.id, "title": chat.title}, event="session")
        elif chat.current_node == "end" or chat.status in {"DONE", "WAITING_APPROVAL", "FAILED"}:
            missing = await db.get(MissingPunch, chat.missing_punch_id) if chat.missing_punch_id else None
            yield sse({"type": "text", "content": "\u672c\u8f6e\u8865\u5361\u6d41\u7a0b\u5df2\u7ecf\u7ed3\u675f\uff0c\u4e0d\u80fd\u7ee7\u7eed\u5728\u8be5\u4f1a\u8bdd\u8f93\u5165\u3002\u8bf7\u70b9\u51fb\u300e\u65b0\u4f1a\u8bdd\u300f\u91cd\u65b0\u5f00\u59cb\u3002", "session_id": chat.id})
            yield sse({"type": "done", "session_id": chat.id, "status": chat.status, "current_node": chat.current_node, "terminal": True, **repair_result_payload(chat.status, chat.current_node, True, missing)}, event="done")
            return
        await add_chat_message(db, chat.id, "user", data.message)
        agent = AutoClockAgent(db, current_user)
        final_status = "ACTIVE"
        current_node = chat.current_node
        async for event in agent.stream_chat_events(data.message, chat.id):
            if event.get("type") == "selected_record":
                selected_id = event["missing_punch_id"]
                result = await db.execute(select(MissingPunch).where(MissingPunch.id == selected_id, MissingPunch.user_id == current_user.id))
                missing = result.scalar_one_or_none()
                if missing:
                    punch_label = "\u4e0a\u73ed\u5361" if missing.punch_type.value == "ON_DUTY" else "\u4e0b\u73ed\u5361"
                    chat = await bind_missing_punch(db, chat, missing.id, f"{missing.work_date:%Y-%m-%d} {punch_label}\u8865\u5361")
                continue
            if event.get("type") == "node":
                current_node = event["node"]
                await update_session_node(db, chat, current_node)
                yield sse({"type": "node", "node": current_node, "session_id": chat.id}, event="node")
                continue
            chunk = event["content"]
            await add_chat_message(db, chat.id, "agent", chunk)
            if "\u81ea\u52a8\u8865\u5361\u5b8c\u6210" in chunk:
                final_status = "DONE"
            elif "\u5df2\u63d0\u4ea4\u9886\u5bfc\u5ba1\u6838" in chunk:
                final_status = "WAITING_APPROVAL"
            elif any(keyword in chunk for keyword in ["\u5931\u8d25", "\u65e0\u6cd5\u521b\u5efa", "\u6ca1\u6709\u627e\u5230\u8fd9\u6761\u53ef\u8865\u5361\u6570\u636e"]):
                final_status = "FAILED"
            yield sse({"type": "text", "content": chunk, "session_id": chat.id})
        terminal = current_node == "end"
        if terminal and final_status == "ACTIVE":
            final_status = "FAILED"
        await finish_session(db, chat, final_status, current_node=current_node)
        missing = await db.get(MissingPunch, chat.missing_punch_id) if chat.missing_punch_id else None
        yield sse({"type": "done", "session_id": chat.id, "status": final_status, "current_node": current_node, "terminal": terminal, **repair_result_payload(final_status, current_node, terminal, missing)}, event="done")
    return StreamingResponse(gen(), media_type="text/event-stream")

@router.post("/agent/start")
async def agent_start(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    async def gen() -> AsyncIterator[str]:
        chat = await create_chat_session(db, current_user, title="补卡会话")
        await add_chat_message(db, chat.id, "user", "开始补卡")
        yield sse({"type": "session", "session_id": chat.id, "title": chat.title}, event="session")
        agent = AutoClockAgent(db, current_user)
        async for chunk in agent.stream_chat("我要补卡", chat.id):
            await add_chat_message(db, chat.id, "agent", chunk)
            yield sse({"type": "text", "content": chunk, "session_id": chat.id})
        yield sse({"type": "done", "session_id": chat.id}, event="done")
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/agent/select")
async def agent_select(data: ChatSelectRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    async def gen() -> AsyncIterator[str]:
        chat = await get_owned_session(db, current_user, data.session_id) if data.session_id else None
        if not chat:
            chat = await create_chat_session(db, current_user, title=f"\u8865\u5361\u8bb0\u5f55 #{data.missing_punch_id}")
            yield sse({"type": "session", "session_id": chat.id, "title": chat.title}, event="session")
        result = await db.execute(select(MissingPunch).where(MissingPunch.id == data.missing_punch_id, MissingPunch.user_id == current_user.id))
        missing = result.scalar_one_or_none()
        if missing:
            punch_label = '\u4e0a\u73ed\u5361' if missing.punch_type.value == 'ON_DUTY' else '\u4e0b\u73ed\u5361'
            title = f"{missing.work_date:%Y-%m-%d} {punch_label}\u8865\u5361"
            chat = await bind_missing_punch(db, chat, missing.id, title)
        await add_chat_message(db, chat.id, "user", f"\u9009\u62e9\u8865\u5361\u8bb0\u5f55 #{data.missing_punch_id}")
        agent = AutoClockAgent(db, current_user)
        final_status = "DONE"
        current_node = chat.current_node
        async for event in agent.stream_select_events(data.missing_punch_id):
            if event.get("type") == "node":
                current_node = event["node"]
                await update_session_node(db, chat, current_node)
                yield sse({"type": "node", "node": current_node, "session_id": chat.id}, event="node")
                continue
            chunk = event["content"]
            await add_chat_message(db, chat.id, "agent", chunk)
            if "\u81ea\u52a8\u8865\u5361\u5b8c\u6210" in chunk:
                final_status = "DONE"
            elif "\u5df2\u63d0\u4ea4\u9886\u5bfc\u5ba1\u6838" in chunk:
                final_status = "WAITING_APPROVAL"
            elif any(keyword in chunk for keyword in ["\u5931\u8d25", "\u65e0\u6cd5\u521b\u5efa", "\u6ca1\u6709\u627e\u5230\u8fd9\u6761\u53ef\u8865\u5361\u6570\u636e"]):
                final_status = "FAILED"
            yield sse({"type": "text", "content": chunk, "session_id": chat.id})
        terminal = current_node == "end"
        if terminal and final_status == "ACTIVE":
            final_status = "FAILED"
        await finish_session(db, chat, final_status, current_node=current_node)
        missing = await db.get(MissingPunch, chat.missing_punch_id) if chat.missing_punch_id else None
        yield sse({"type": "done", "session_id": chat.id, "status": final_status, "current_node": current_node, "terminal": terminal, **repair_result_payload(final_status, current_node, terminal, missing)}, event="done")
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/approvals", response_model=list[ApprovalOut])
async def approvals(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await list_leader_approvals(db, current_user)


@router.post("/approvals/{approval_id}/review")
async def review(approval_id: int, data: ApprovalReviewRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    result = await db.execute(select(RepairApproval).where(RepairApproval.id == approval_id, RepairApproval.leader_id == current_user.id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="审核单不存在")
    if approval.status != ApprovalStatus.pending:
        raise HTTPException(status_code=400, detail="审核单已处理")
    missing_punch_id = approval.missing_punch_id
    approval = await review_approval(db, approval, current_user, data.approved, data.comment)
    await sync_sessions_for_missing_punch(db, missing_punch_id, "DONE" if data.approved else "FAILED", approval.id)
    return {"id": approval.id, "status": approval.status.value}




