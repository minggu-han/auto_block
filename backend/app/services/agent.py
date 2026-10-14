from __future__ import annotations

from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.attendance import MissingPunch, MissingPunchStatus
from app.models.chat import ChatMessage, ChatSession
from app.models.user import User
from app.services.attendance_service import ensure_pending_approval, list_my_missing_punches, repair_missing_punch
from app.services.chat_service import bind_missing_punch

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - 允许未安装依赖时后端仍可启动并提示配置问题
    ChatOpenAI = None


def _punch_label(value: str) -> str:
    return "上班卡" if value == "ON_DUTY" else "下班卡"


# 滑动窗口大小：最近 HISTORY_WINDOW 条消息原文保留，更早的压缩进会话 summary。
HISTORY_WINDOW = 8


# ReAct Agent 的系统提示词：告诉模型何时查数据、何时触发补卡，以及严禁编造补卡记录 id。
SYSTEM_PROMPT = """你是公司的自动补卡 Agent，帮助员工查询并处理忘记打卡的数据。

你可以使用以下工具：
- query_missing_punches：查询当前用户所有待补卡数据（无需参数）。
- repair_missing_punch(missing_punch_id)：对某条补卡记录执行补卡流程（自动补卡或提交领导审核），返回处理结果。
- get_current_date：查询今天的日期和星期（无需参数）。

规则：
1. 用户表达想补卡、忘记打卡、漏打卡，但没有明确选择某条数据时，先调用 query_missing_punches，再把返回的列表原样展示给用户，并提示用户按编号或第几条选择。
2. 用户用“编号 N / 第 N 条 / 日期 + 上班卡/下班卡”等方式选择时，从上面查询结果里找到对应“编号”后面的数字 id，再调用 repair_missing_punch(id)。绝不能编造一个查询结果里不存在的 id。
3. 用户提到“今天/昨天/明天/本周/上周/周几”等相对日期，或需要知道当前日期时，先调用 get_current_date 获取今天日期再据此推断。
4. 调用 repair_missing_punch 后，如实把工具返回的结果转述给用户，不要虚构补卡结果。
5. 其它闲聊或不清楚的输入，简短友好地回复即可，不要调用工具。
"""


class AutoClockAgent:
    """基于 OpenAI 兼容接口 + LangGraph ReAct 的自动补卡对话 Agent。

    LLM 通过工具自主决定下一步：
    - 用户表达“我要补卡/忘记打卡”时，调用 query_missing_punches 查询待补卡数据并展示。
    - 用户回复“第 1 条 / 选 12 / 补昨天上班卡”等时，调用 repair_missing_punch 触发补卡流程。
    - 补卡 / 提交审核等关键动作仍由后端确定性代码执行（查领导 -> 自动补卡 or 提交审核），
      LLM 只负责“何时查、何时触发”，不决定业务规则。
    """

    def __init__(self, db: AsyncSession, user: User):
        self.db = db
        self.user = user
        # 本轮结果，供 API 层构造最终 done 事件：
        #   terminal=True 时 status 为 DONE/WAITING_APPROVAL/FAILED；terminal=False 时为 ACTIVE。
        self.outcome: dict | None = None
        # 本轮实际调用过的工具名，用于非终态时决定 current_node。
        self.tools_used: set[str] = set()

    async def _open_items(self) -> list[MissingPunch]:
        # 工具能力：查询当前用户还可以处理的补卡记录。
        # only_open=True 只返回 PENDING / APPROVAL_PENDING 状态的数据。
        return await list_my_missing_punches(self.db, self.user, only_open=True)

    def _format_items(self, items: list[MissingPunch]) -> str:
        if not items:
            return "你当前没有待补卡数据。请先在『忘记打卡录入』页面新增一条数据。"
        lines = ["我调用工具查询到了你当前所有可补卡的数据，请回复编号或第几条来选择："]
        for index, item in enumerate(items, start=1):
            lines.append(f"{index}. 编号 {item.id}｜{item.work_date:%Y-%m-%d}｜{_punch_label(item.punch_type.value)}｜状态 {item.status.value}")
        lines.append("例如：回复“选第 1 条”或“补编号 12”。")
        return "\n".join(lines)

    async def _load_window_messages(self, session_id: int | None) -> list[ChatMessage]:
        # 滑动窗口：只取最近 HISTORY_WINDOW 条原文，更早的由 chat.summary 摘要承接。
        if not session_id:
            return []
        result = await self.db.execute(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id.desc()).limit(HISTORY_WINDOW)
        )
        return list(reversed(result.scalars().all()))

    def _make_model(self):
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            temperature=0,
        )

    async def _summarize_history(self, existing_summary: str | None, overflow: list[ChatMessage]) -> str:
        # 把已有摘要和滑出窗口的旧消息合并成一段简洁摘要，控制后续上下文长度。
        lines = "\n".join(f"{m.role}: {m.content}" for m in overflow)
        prompt = (
            "你是对话摘要器。请把下面「已有摘要」和「新增对话」合并成一段简洁摘要（不超过 150 字），"
            "保留关键信息：用户想补的卡、已选择的补卡记录编号、补卡结果 / 审核状态等。\n\n"
            f"已有摘要：\n{existing_summary or '（无）'}\n\n"
            f"新增对话：\n{lines}\n\n"
            "只输出摘要本身，不要额外解释。"
        )
        resp = await self._make_model().ainvoke([HumanMessage(content=prompt)])
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        return content.strip()

    async def refresh_summary(self, chat: ChatSession) -> None:
        # 会话消息超过滑动窗口后，把窗口之外的旧消息压缩进 chat.summary。
        result = await self.db.execute(select(ChatMessage).where(ChatMessage.session_id == chat.id).order_by(ChatMessage.id.asc()))
        msgs = list(result.scalars().all())
        if len(msgs) <= HISTORY_WINDOW:
            chat.summary = None
        else:
            overflow = msgs[:-HISTORY_WINDOW]
            try:
                chat.summary = await self._summarize_history(chat.summary, overflow)
            except Exception:
                # 摘要失败不影响主流程，保留旧摘要即可。
                pass
        await self.db.commit()

    async def _execute_repair(self, missing_punch_id: int, chat: ChatSession) -> str:
        # 确定性补卡流程（合并原先 load_selected_record / check_leader_policy / auto_repair / request_leader_approval 四个节点）：
        # 1. 校验记录属于本人且状态允许处理
        # 2. 把聊天会话绑定到该补卡记录
        # 3. 查领导配置 -> 自动补卡 或 提交审核
        result = await self.db.execute(
            select(MissingPunch).where(
                MissingPunch.id == missing_punch_id,
                MissingPunch.user_id == self.user.id,
                MissingPunch.status.in_([MissingPunchStatus.pending, MissingPunchStatus.approval_pending]),
            )
        )
        missing = result.scalar_one_or_none()
        if not missing:
            self.outcome = {"terminal": True, "status": "FAILED", "missing_punch_id": None}
            return "没有找到这条可补卡数据，可能已经补卡或被拒绝，请重新说“我要补卡”查询最新数据。"

        punch_label = _punch_label(missing.punch_type.value)
        await bind_missing_punch(self.db, chat, missing.id, f"{missing.work_date:%Y-%m-%d} {punch_label}补卡")

        if not self.user.leader_id:
            await repair_missing_punch(self.db, missing, self.user.id, "Agent 自动补卡")
            self.outcome = {"terminal": True, "status": "DONE", "missing_punch_id": missing.id}
            return f"✅ 自动补卡完成：{missing.work_date:%Y-%m-%d} {punch_label}。（未配置领导，直接自动补卡）"

        leader_result = await self.db.execute(select(User).where(User.id == self.user.leader_id, User.is_active.is_(True)))
        leader = leader_result.scalar_one_or_none()
        if not leader:
            await repair_missing_punch(self.db, missing, self.user.id, "Agent 自动补卡")
            self.outcome = {"terminal": True, "status": "DONE", "missing_punch_id": missing.id}
            return f"✅ 自动补卡完成：{missing.work_date:%Y-%m-%d} {punch_label}。（未找到有效领导，直接自动补卡）"

        if leader.attendance_auto_audit_enabled:
            await repair_missing_punch(self.db, missing, self.user.id, "Agent 自动补卡")
            self.outcome = {"terminal": True, "status": "DONE", "missing_punch_id": missing.id}
            return f"✅ 自动补卡完成：{missing.work_date:%Y-%m-%d} {punch_label}。（领导 {leader.full_name} 已开启自动审核）"

        approval = await ensure_pending_approval(self.db, missing, self.user, leader)
        self.outcome = {"terminal": True, "status": "WAITING_APPROVAL", "missing_punch_id": missing.id}
        return f"🕒 已提交领导审核，审核单号：{approval.id}。领导通过后系统会自动补卡，你也可以稍后回到本页面刷新状态。"

    async def stream_chat_events(self, message: str, chat: ChatSession):
        # 统一聊天入口：构造 ReAct Agent，把结果转成事件供 API 层推 SSE。
        self.outcome = None
        self.tools_used = set()

        if ChatOpenAI is None or not settings.openai_api_key:
            yield {"type": "text", "content": "模型未配置，无法使用智能补卡助手。请检查后端 .env 的 OPENAI_API_KEY / OPENAI_BASE_URL。"}
            self.outcome = {"terminal": True, "status": "FAILED", "missing_punch_id": None}
            return

        model = self._make_model()

        # 用闭包定义工具并捕获 self / chat，避免 @tool 直接装饰方法时把 self 当成工具参数。
        @tool
        async def query_missing_punches() -> str:
            """查询当前用户所有待补卡的打卡数据，返回可选择的记录列表。"""
            self.tools_used.add("query_missing_punches")
            return self._format_items(await self._open_items())

        @tool
        async def repair_missing_punch(missing_punch_id: int) -> str:
            """对指定编号的补卡记录执行补卡流程（自动补卡或提交领导审核），返回处理结果。"""
            self.tools_used.add("repair_missing_punch")
            return await self._execute_repair(missing_punch_id, chat)

        @tool
        async def get_current_date() -> str:
            """查询今天的日期和星期，返回 YYYY-MM-DD 和星期几。"""
            weekdays = ["一", "二", "三", "四", "五", "六", "日"]
            now = datetime.now()
            return f"今天是 {now:%Y-%m-%d}（星期{weekdays[now.weekday()]}）"

        react = create_react_agent(model, [query_missing_punches, repair_missing_punch, get_current_date], prompt=SYSTEM_PROMPT)

        window = await self._load_window_messages(chat.id)
        prior = [HumanMessage(content=m.content) if m.role == "user" else AIMessage(content=m.content) for m in window]
        prefix = [SystemMessage(content=f"以下是之前对话的摘要，供你理解上下文：\n{chat.summary}")] if chat.summary else []
        inputs = {"messages": prefix + prior + [HumanMessage(content=message)]}

        try:
            async for step in react.astream(inputs, stream_mode="updates"):
                for update in step.values():
                    for m in update.get("messages", []):
                        if isinstance(m, AIMessage):
                            if m.content:
                                yield {"type": "text", "content": m.content}
                            for tc in m.tool_calls or []:
                                yield {"type": "node", "node": tc.get("name") or "tool"}
        except Exception:
            # 模型不可用或调用失败时给出友好提示，不把异常抛到接口层。
            yield {"type": "text", "content": "模型调用失败，请稍后重试。"}
            self.outcome = {"terminal": True, "status": "FAILED", "missing_punch_id": None}
            return

        # 本轮没有触发补卡（只是列数据或闲聊），按用过的工具决定非终态节点。
        if self.outcome is None:
            if "query_missing_punches" in self.tools_used:
                self.outcome = {"terminal": False, "status": "ACTIVE", "missing_punch_id": None, "current_node": "waiting_user_selection"}
            else:
                self.outcome = {"terminal": False, "status": "ACTIVE", "missing_punch_id": None, "current_node": "waiting_user_intent"}
