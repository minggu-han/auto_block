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
from app.services.chat_service import add_chat_message, create_chat_session, finish_session, get_chat_session_detail, get_owned_session, list_chat_sessions, sync_sessions_for_missing_punch, update_session_node

router = APIRouter(prefix="/api")


# 将普通 dict 包装成 SSE 文本格式。
# 前端通过 fetch reader 按 "\n\n" 拆分事件。
def sse(data: dict | str, event: str = "message") -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, default=str)
    print(f'event: {event}\ndata: {payload}\n\n')
    return f"event: {event}\ndata: {payload}\n\n"


# 统一生成“本轮补卡结果”里的补卡记录信息，前端结果卡片直接使用。
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


# 根据会话状态生成结果文案：成功、等待审核、失败、处理中。
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
    # 补卡聊天主接口（ReAct Agent）：
    # 1. 创建或恢复聊天会话
    # 2. 保存用户消息
    # 3. 由 ReAct Agent 自主决定调用工具（查询 / 补卡）
    # 4. 以 SSE 流式返回文本与节点事件
    async def gen() -> AsyncIterator[str]:
        # 首次对话 session_id 为 None
        chat = await get_owned_session(db, current_user, data.session_id) if data.session_id else None
        if not chat:
            # 首次对话
            title = data.message[:40] if data.message else "补卡会话"
            chat = await create_chat_session(db, current_user, title=title or "补卡会话")
            yield sse({"type": "session", "session_id": chat.id, "title": chat.title}, event="session")
        elif chat.current_node == "end" or chat.status in {"DONE", "WAITING_APPROVAL", "FAILED"}:
            # 后端兜底：已结束的会话不允许继续聊天，避免绕过前端禁用输入框。
            missing = await db.get(MissingPunch, chat.missing_punch_id) if chat.missing_punch_id else None
            yield sse({"type": "text", "content": "本轮补卡流程已经结束，不能继续在该会话输入。请点击『新会话』重新开始。", "session_id": chat.id})
            yield sse({"type": "done", "session_id": chat.id, "status": chat.status, "current_node": chat.current_node, "terminal": True, **repair_result_payload(chat.status, chat.current_node, True, missing)}, event="done")
            return
        await add_chat_message(db, chat.id, "user", data.message)
        agent = AutoClockAgent(db, current_user)
        async for event in agent.stream_chat_events(data.message, chat):
            if event.get("type") == "node":
                # 节点事件只更新 current_node，不作为聊天消息保存。
                await update_session_node(db, chat, event["node"])
                yield sse({"type": "node", "node": event["node"], "session_id": chat.id}, event="node")
                continue
            # 文本事件：保存为 agent 消息并透传给前端。
            await add_chat_message(db, chat.id, "agent", event["content"])
            yield sse({"type": "text", "content": event["content"], "session_id": chat.id})
        outcome = agent.outcome or {}
        terminal = bool(outcome.get("terminal"))
        final_status = outcome.get("status", "ACTIVE")
        current_node = "end" if terminal else outcome.get("current_node", "waiting_user_intent")
        await finish_session(db, chat, final_status, current_node=current_node)
        # 滑动窗口 + 摘要：窗口之外的旧消息压缩进 chat.summary，控制下轮上下文长度。
        await agent.refresh_summary(chat)
        missing = await db.get(MissingPunch, chat.missing_punch_id) if chat.missing_punch_id else None
        yield sse({"type": "done", "session_id": chat.id, "status": final_status, "current_node": current_node, "terminal": terminal, **repair_result_payload(final_status, current_node, terminal, missing)}, event="done")
    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/approvals", response_model=list[ApprovalOut])
async def approvals(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return await list_leader_approvals(db, current_user)


@router.post("/approvals/{approval_id}/review")
async def review(approval_id: int, data: ApprovalReviewRequest, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    # 领导审核接口：审核通过会触发自动补卡；审核拒绝会把补卡记录标记为 REJECTED。
    # 同时需要同步关联聊天会话状态，否则员工回到聊天页仍会看到“等待领导审核”。
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




