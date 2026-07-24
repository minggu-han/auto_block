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
    punch_label = "上班卡" if missing.punch_type.value == "ON_DUTY" else "下班卡"
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
        payload.update({"result_status": "IN_PROGRESS", "result_message": "本轮对话尚未结束，请继续按提示操作。"})
    elif status == "DONE":
        payload.update({"result_status": "SUCCESS", "result_message": "补卡成功，当前记录已自动补卡完成。"})
    elif status == "WAITING_APPROVAL":
        payload.update({"result_status": "PENDING_APPROVAL", "result_message": "已提交领导审核，领导通过后会自动补卡。"})
    elif status == "FAILED":
        payload.update({"result_status": "FAILED", "result_message": "补卡失败，请查看聊天内容中的失败原因。"})
    else:
        payload.update({"result_status": "FAILED", "result_message": "本轮补卡流程已结束，但没有完成补卡，请查看聊天内容确认原因。"})
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
            yield sse({"type": "text", "content": "本轮补卡流程已经结束，不能继续在该会话输入。请点击『新会话』重新开始。", "session_id": chat.id})
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
                    punch_label = "上班卡" if missing.punch_type.value == "ON_DUTY" else "下班卡"
                    chat = await bind_missing_punch(db, chat, missing.id, f"{missing.work_date:%Y-%m-%d} {punch_label}补卡")
                continue
            if event.get("type") == "node":
                current_node = event["node"]
                await update_session_node(db, chat, current_node)
                yield sse({"type": "node", "node": current_node, "session_id": chat.id}, event="node")
                continue
            chunk = event["content"]
            await add_chat_message(db, chat.id, "agent", chunk)
            if "自动补卡完成" in chunk:
                final_status = "DONE"
            elif "已提交领导审核" in chunk:
                final_status = "WAITING_APPROVAL"
            elif any(keyword in chunk for keyword in ["失败", "无法创建", "没有找到这条可补卡数据"]):
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
            chat = await create_chat_session(db, current_user, title=f"补卡记录 #{data.missing_punch_id}")
            yield sse({"type": "session", "session_id": chat.id, "title": chat.title}, event="session")
        result = await db.execute(select(MissingPunch).where(MissingPunch.id == data.missing_punch_id, MissingPunch.user_id == current_user.id))
        missing = result.scalar_one_or_none()
        if missing:
            punch_label = '上班卡' if missing.punch_type.value == 'ON_DUTY' else '下班卡'
            title = f"{missing.work_date:%Y-%m-%d} {punch_label}补卡"
            chat = await bind_missing_punch(db, chat, missing.id, title)
        await add_chat_message(db, chat.id, "user", f"选择补卡记录 #{data.missing_punch_id}")
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
            if "自动补卡完成" in chunk:
                final_status = "DONE"
            elif "已提交领导审核" in chunk:
                final_status = "WAITING_APPROVAL"
            elif any(keyword in chunk for keyword in ["失败", "无法创建", "没有找到这条可补卡数据"]):
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




