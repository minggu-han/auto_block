from __future__ import annotations

import asyncio
import json
import re
from operator import add
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.attendance import MissingPunch, MissingPunchStatus
from app.models.chat import ChatMessage
from app.models.user import User
from app.services.attendance_service import ensure_pending_approval, list_my_missing_punches, repair_missing_punch

try:
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - 允许未安装依赖时后端仍可启动并提示配置问题
    ChatOpenAI = None


def _punch_label(value: str) -> str:
    return "上班卡" if value == "ON_DUTY" else "下班卡"


# LangGraph 中流转的状态对象。
# 每个节点会读取/写入这里的字段，比如 missing_punch_id、next、leader_id 等。
class RepairState(TypedDict, total=False):
    user_id: int
    user_message: str
    missing_punch_id: int | None
    messages: Annotated[list[AIMessage], add]
    next: str
    leader_id: int | None
    approval_id: int | None


# OpenAI 意图理解后的标准输出结构。
# 后续业务逻辑只认这三种 intent，避免直接依赖大模型自由文本。
class UserIntent(TypedDict):
    intent: Literal["list_records", "select_record", "other"]
    selected_id: int | None
    reply: str | None


class AutoClockAgent:
    """基于 OpenAI + LangGraph 的自动补卡对话 Agent。

    LLM 负责理解用户自然语言：
    - 用户表达“我要补卡/忘记打卡”时，Agent 调用查询待补卡数据工具并在聊天框展示。
    - 用户回复“第 1 条 / 选 12 / 补昨天上班卡”等时，LLM 从候选数据中理解所选记录。
    - 选定后进入 LangGraph 后续节点：查领导 -> 判断自动审核 -> 自动补卡或提交审核。
    """

    def __init__(self, db: AsyncSession, user: User):
        self.db = db
        self.user = user
        self._select_graph = self._build_select_graph()

    def _build_select_graph(self):
        # 这里定义真正的补卡流程图：
        # 1. 加载用户选择的补卡记录
        # 2. 查询领导和审核配置
        # 3. 根据配置进入自动补卡或提交审核
        graph = StateGraph(RepairState)
        graph.add_node("load_selected_record", self.load_selected_record)
        graph.add_node("check_leader_policy", self.check_leader_policy)
        graph.add_node("auto_repair", self.auto_repair)
        graph.add_node("request_leader_approval", self.request_leader_approval)
        graph.set_entry_point("load_selected_record")
        graph.add_conditional_edges(
            "load_selected_record",
            lambda state: state.get("next", "check_leader_policy"),
            {"check_leader_policy": "check_leader_policy", "end": END},
        )
        graph.add_conditional_edges(
            "check_leader_policy",
            lambda state: state["next"],
            {"auto_repair": "auto_repair", "request_leader_approval": "request_leader_approval"},
        )
        graph.add_edge("auto_repair", END)
        graph.add_edge("request_leader_approval", END)
        return graph.compile()

    async def _emit(self, text: str):
        await asyncio.sleep(0.05)
        return AIMessage(content=text)

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

    async def _load_recent_messages(self, session_id: int | None) -> list[ChatMessage]:
        # 加载最近聊天历史给 OpenAI，解决“选第1条”这类依赖上下文的问题。
        if not session_id:
            return []
        result = await self.db.execute(select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id.desc()).limit(12))
        return list(reversed(result.scalars().all()))

    def _heuristic_intent(self, message: str, items: list[MissingPunch]) -> UserIntent:
        # OpenAI 未配置或调用失败时的兜底规则。
        # 只做简单关键词/编号识别，保证系统最低限度可用。
        text = message.strip()
        if re.search(r"(补卡|忘记打卡|漏打卡|缺卡|打卡)", text):
            id_match = re.search(r"(?:编号|#)\s*(\d+)", text)
            if id_match:
                return {"intent": "select_record", "selected_id": int(id_match.group(1)), "reply": None}
            nth_match = re.search(r"第\s*(\d+)\s*条", text)
            if nth_match:
                idx = int(nth_match.group(1)) - 1
                if 0 <= idx < len(items):
                    return {"intent": "select_record", "selected_id": items[idx].id, "reply": None}
            return {"intent": "list_records", "selected_id": None, "reply": None}
        id_match = re.search(r"(?:编号|#|选|选择|第)?\s*(\d+)\s*(?:条)?", text)
        if id_match and items:
            n = int(id_match.group(1))
            if any(item.id == n for item in items):
                return {"intent": "select_record", "selected_id": n, "reply": None}
            if 1 <= n <= len(items):
                return {"intent": "select_record", "selected_id": items[n - 1].id, "reply": None}
        return {"intent": "other", "selected_id": None, "reply": "我可以帮你处理补卡。请直接告诉我“我要补卡”，我会查询你的待补卡数据。"}

    async def understand_user_message(self, message: str, session_id: int | None = None) -> UserIntent:
        # 使用 OpenAI 把用户自然语言转换为结构化意图。
        # 注意：大模型只负责“理解”，真正查询/补卡/审核仍由后端工具函数执行。
        items = await self._open_items()
        history = await self._load_recent_messages(session_id)
        if not settings.openai_api_key or ChatOpenAI is None:
            return self._heuristic_intent(message, items)

        item_text = "\n".join(
            f"第{idx}条: id={item.id}, date={item.work_date:%Y-%m-%d}, type={_punch_label(item.punch_type.value)}, status={item.status.value}"
            for idx, item in enumerate(items, start=1)
        ) or "无待补卡数据"
        history_text = "\n".join(f"{m.role}: {m.content}" for m in history[-8:]) or "无"
        system = """你是公司自动补卡 Agent 的意图理解器。只输出 JSON，不要输出 markdown。
可用工具：query_missing_punches（查询当前用户所有待补卡数据）、select_missing_punch（选择一条待补卡数据进入补卡流程）。
判断规则：
1. 用户表达想补卡、忘记打卡、漏打卡，且没有明确选择某条数据 => intent=list_records。
2. 用户用编号、序号、第几条、日期+上班/下班等方式选择候选数据 => intent=select_record，并返回 selected_id。
3. 其它闲聊或不清楚 => intent=other，并给一句简短 reply。
返回格式：{"intent":"list_records|select_record|other","selected_id":数字或null,"reply":"字符串或null"}
"""
        prompt = f"""聊天历史：
{history_text}

当前工具 query_missing_punches 可返回的候选数据：
{item_text}

用户最新输入：{message}
"""
        llm = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            temperature=0,
        )
        try:
            resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=prompt)])
            #将 LLM 响应内容转换为字符串
            #   如果 resp.content 已经是字符串 → 直接使用
            #   如果 resp.content 是其他类型（如列表、字典）→ 使用 json.dumps() 转为 JSON 字符串
            #   ensure_ascii=False：保留非 ASCII 字符（如中文），不转义为 \uXXXX
            raw = resp.content if isinstance(resp.content, str) else json.dumps(resp.content, ensure_ascii=False)
            #使用正则表达式在 raw 字符串中搜索 JSON 对象
            # 正则解析：
            # 1、    \{：匹配左花括号
            # 2、   . *：匹配任意字符（0个或多个）
            # 3、    \}：匹配右花括号
            # 4、    re.S 标志：让.匹配换行符，实现跨行匹配
            # match 是匹配对象，如果找到返回匹配对象，否则返回 None
            match = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(match.group(0) if match else raw)
            intent = data.get("intent")
            selected_id = data.get("selected_id")
            if selected_id is not None:
                selected_id = int(selected_id)
            if intent in {"list_records", "select_record", "other"}:
                return {"intent": intent, "selected_id": selected_id, "reply": data.get("reply")}
        except Exception:
            return self._heuristic_intent(message, items)
        return self._heuristic_intent(message, items)

    async def stream_chat(self, message: str, session_id: int | None = None):
        async for event in self.stream_chat_events(message, session_id):
            if event.get("type") == "text":
                yield event["content"]

    async def stream_chat_events(self, message: str, session_id: int | None = None):
        # 统一聊天入口：先理解意图，再按意图分发。
        # 这里产出的事件会被 API 层转成 SSE 推给前端。
        yield {"type": "node", "node": "understand_user_message"}
        intent = await self.understand_user_message(message, session_id)
        if intent["intent"] == "list_records":
            yield {"type": "node", "node": "query_missing_punches"}
            yield {"type": "text", "content": self._format_items(await self._open_items())}
            yield {"type": "node", "node": "waiting_user_selection"}
            return
        if intent["intent"] == "select_record" and intent.get("selected_id"):
            selected_id = int(intent["selected_id"])
            yield {"type": "selected_record", "missing_punch_id": selected_id}
            async for event in self.stream_select_events(selected_id):
                yield event
            return
        yield {"type": "node", "node": "waiting_user_intent"}
        yield {"type": "text", "content": intent.get("reply") or "我可以帮你处理补卡。请告诉我“我要补卡”，或在我列出数据后回复要补哪一条。"}

    async def load_selected_record(self, state: RepairState) -> RepairState:
        # LangGraph 节点1：校验用户选择的补卡记录是否存在、是否属于本人、状态是否允许处理。
        missing_id = state.get("missing_punch_id")
        result = await self.db.execute(
            select(MissingPunch).where(
                MissingPunch.id == missing_id,
                MissingPunch.user_id == self.user.id,
                MissingPunch.status.in_([MissingPunchStatus.pending, MissingPunchStatus.approval_pending]),
            )
        )
        missing = result.scalar_one_or_none()
        if not missing:
            msg = await self._emit("没有找到这条可补卡数据，可能已经补卡或被拒绝，请重新说“我要补卡”查询最新数据。")
            return {"messages": [msg], "next": "end"}
        msg = await self._emit(f"已选择：{missing.work_date:%Y-%m-%d} {_punch_label(missing.punch_type.value)}。接下来查询你的领导和审核配置。")
        return {"messages": [msg], "next": "check_leader_policy"}

    async def check_leader_policy(self, state: RepairState) -> RepairState:
        # LangGraph 节点2：查询领导，并根据领导是否开启自动审核决定下一步。
        if not self.user.leader_id:
            msg = await self._emit("未配置领导，无法进入审核；系统将按无领导场景直接自动补卡。")
            return {"messages": [msg], "next": "auto_repair", "leader_id": None}
        result = await self.db.execute(select(User).where(User.id == self.user.leader_id, User.is_active.is_(True)))
        leader = result.scalar_one_or_none()
        if not leader:
            msg = await self._emit("未找到有效领导，系统将直接自动补卡。")
            return {"messages": [msg], "next": "auto_repair", "leader_id": None}
        if leader.attendance_auto_audit_enabled:
            msg = await self._emit(f"你的领导是 {leader.full_name}，TA 已开启自动审核打卡，将直接进入自动补卡节点。")
            return {"messages": [msg], "next": "auto_repair", "leader_id": leader.id}
        msg = await self._emit(f"你的领导是 {leader.full_name}，TA 未开启自动审核打卡，需要先提交领导审核。")
        return {"messages": [msg], "next": "request_leader_approval", "leader_id": leader.id}

    async def auto_repair(self, state: RepairState) -> RepairState:
        # LangGraph 节点3A：自动补卡。当前实现是更新本系统数据库状态，尚未接入真实考勤系统。
        result = await self.db.execute(select(MissingPunch).where(MissingPunch.id == state.get("missing_punch_id"), MissingPunch.user_id == self.user.id))
        missing = result.scalar_one_or_none()
        if not missing:
            return {"messages": [AIMessage(content="自动补卡失败：记录不存在。")]} 
        await repair_missing_punch(self.db, missing, self.user.id, "Agent 自动补卡")
        msg = await self._emit(f"✅ 自动补卡完成：{missing.work_date:%Y-%m-%d} {_punch_label(missing.punch_type.value)}。")
        return {"messages": [msg]}

    async def request_leader_approval(self, state: RepairState) -> RepairState:
        # LangGraph 节点3B：领导未开启自动审核时，创建或复用待审核单。
        leader_id = state.get("leader_id")
        if not leader_id:
            return {"messages": [AIMessage(content="无法创建审核单：领导不存在。")]} 
        result = await self.db.execute(select(MissingPunch).where(MissingPunch.id == state.get("missing_punch_id"), MissingPunch.user_id == self.user.id))
        missing = result.scalar_one_or_none()
        if not missing:
            return {"messages": [AIMessage(content="无法创建审核单：补卡记录不存在。")]} 
        leader = (await self.db.execute(select(User).where(User.id == leader_id))).scalar_one()
        approval = await ensure_pending_approval(self.db, missing, self.user, leader)
        msg = await self._emit(f"🕒 已提交领导审核，审核单号：{approval.id}。领导通过后系统会自动补卡，你也可以稍后回到本页面刷新状态。")
        return {"messages": [msg], "approval_id": approval.id}

    async def stream_select(self, missing_punch_id: int):
        async for event in self.stream_select_events(missing_punch_id):
            if event.get("type") == "text":
                yield event["content"]

    async def stream_select_events(self, missing_punch_id: int):
        # 执行 LangGraph，并把每个节点名与节点输出文本转换成事件，方便前端实时展示流程节点。
        async for event in self._select_graph.astream({"user_id": self.user.id, "missing_punch_id": missing_punch_id}):
            for node_name, state in event.items():
                yield {"type": "node", "node": node_name}
                for msg in state.get("messages", []):
                    yield {"type": "text", "content": msg.content}
        yield {"type": "node", "node": "end"}

