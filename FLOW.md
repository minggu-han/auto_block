# 自动补卡 Agent 流程说明

本文档说明当前项目中自动补卡的整体业务流程、技术实现和关键细节。

## 1. 项目技术栈

### 前端

- Vue 3
- Vite
- Vue Router
- Pinia
- Axios
- Fetch + SSE 流式读取

主要页面：

- `frontend/src/views/Login.vue`：登录页
- `frontend/src/views/MissingPunch.vue`：忘记打卡录入页
- `frontend/src/views/RepairChat.vue`：补卡聊天页
- `frontend/src/views/Approvals.vue`：领导审核页

### 后端

- Python
- FastAPI
- SQLAlchemy Async ORM
- asyncpg
- PostgreSQL
- LangChain
- LangGraph
- langchain-openai
- OpenAI Chat Model
- SSE 流式响应

主要后端文件：

- `backend/app/api/routes.py`：接口路由
- `backend/app/services/agent.py`：OpenAI + LangGraph 补卡 Agent
- `backend/app/services/attendance_service.py`：打卡、补卡、审核业务逻辑
- `backend/app/services/chat_service.py`：聊天会话与聊天记录逻辑
- `backend/app/models/attendance.py`：补卡与审核相关模型
- `backend/app/models/chat.py`：聊天记录模型
- `backend/app/models/user.py`：用户与领导关系模型

### 数据库

- PostgreSQL
- 表前缀：`auto_block_`
- 启动项目不会自动建表
- DDL 脚本在 `ddl` 目录中

默认数据库连接：

```env
DATABASE_URL=postgresql+asyncpg://mcp:mcp@172.18.1.76:5432/mcp
```

OpenAI 配置：

```env
OPENAI_API_KEY=你的OpenAI Key
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=
```

如果使用 OpenAI 兼容网关，可以填写 `OPENAI_BASE_URL`。

---

## 2. 数据库核心表

### 2.1 用户表：`auto_block_users`

用于保存员工、领导和审核配置。

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 用户 ID |
| `username` | 登录用户名 |
| `full_name` | 姓名 |
| `password_hash` | 密码哈希 |
| `leader_id` | 当前用户的领导 ID |
| `attendance_auto_audit_enabled` | 作为领导时，是否开启打卡自动审核 |
| `is_active` | 是否启用 |

关键逻辑：

- 员工通过 `leader_id` 找到自己的领导。
- 领导通过 `attendance_auto_audit_enabled` 控制下属补卡是否需要人工审核。
- 如果领导开启自动审核，下属补卡会直接自动完成。
- 如果领导未开启自动审核，下属补卡会提交审核单。

---

### 2.2 忘记打卡表：`auto_block_missing_punches`

用于保存员工忘记打卡的数据。

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 忘记打卡记录 ID |
| `user_id` | 员工 ID |
| `work_date` | 忘记打卡日期，格式 `yyyy-MM-dd` |
| `punch_type` | 上班卡或下班卡 |
| `status` | 当前状态 |
| `created_at` | 创建时间 |
| `repaired_at` | 补卡完成时间 |

`punch_type` 枚举：

| 值 | 说明 |
|---|---|
| `ON_DUTY` | 上班卡 |
| `OFF_DUTY` | 下班卡 |

`status` 枚举：

| 值 | 说明 |
|---|---|
| `PENDING` | 待补卡 |
| `APPROVAL_PENDING` | 等待领导审核 |
| `REPAIRED` | 已补卡 |
| `REJECTED` | 已拒绝 |

当前版本中，忘记打卡录入页面只需要填写：

- 日期
- 上班卡 / 下班卡

不需要填写原因。

---

### 2.3 审核表：`auto_block_repair_approvals`

用于保存领导审核单。

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 审核单 ID |
| `missing_punch_id` | 对应的忘记打卡记录 ID |
| `applicant_id` | 申请人 ID |
| `leader_id` | 审核领导 ID |
| `status` | 审核状态 |
| `comment` | 审核意见 |
| `created_at` | 创建时间 |
| `reviewed_at` | 审核时间 |

`status` 枚举：

| 值 | 说明 |
|---|---|
| `PENDING` | 待审核 |
| `APPROVED` | 已通过 |
| `REJECTED` | 已拒绝 |

---

### 2.4 补卡日志表：`auto_block_repair_logs`

用于记录补卡流程中的关键操作。

常见动作：

| action | 说明 |
|---|---|
| `REPAIR` | 执行补卡 |
| `REQUEST_APPROVAL` | 提交领导审核 |
| `REJECT` | 领导拒绝 |

---

### 2.5 聊天会话表：`auto_block_chat_sessions`

用于保存每一轮补卡聊天会话。

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 会话 ID |
| `user_id` | 当前聊天用户 ID |
| `missing_punch_id` | 本次会话针对的补卡记录 ID |
| `title` | 会话标题 |
| `status` | 会话状态 |
| `current_node` | 当前流程节点 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

会话状态：

| 值 | 说明 |
|---|---|
| `ACTIVE` | 进行中 |
| `DONE` | 补卡成功，流程已结束 |
| `WAITING_APPROVAL` | 已提交领导审核，等待领导处理 |
| `FAILED` | 补卡失败或领导拒绝 |

`current_node` 用于前端展示当前流程走到了哪个节点。

---

### 2.6 聊天消息表：`auto_block_chat_messages`

用于保存每条聊天消息。

核心字段：

| 字段 | 说明 |
|---|---|
| `id` | 消息 ID |
| `session_id` | 所属会话 ID |
| `role` | 消息角色 |
| `content` | 消息内容 |
| `created_at` | 创建时间 |

`role` 当前主要有：

| role | 说明 |
|---|---|
| `user` | 用户消息 |
| `agent` | Agent 消息 |

---

## 3. 页面流程

### 3.1 登录

用户访问系统后先登录。

接口：

```http
POST /api/auth/login
```

登录成功后，前端把 token 保存到 `localStorage`，后续请求自动携带：

```http
Authorization: Bearer <token>
```

当前用户信息接口：

```http
GET /api/users/me
```

---

### 3.2 忘记打卡录入

页面：

```text
frontend/src/views/MissingPunch.vue
```

用户录入：

- 忘记打卡日期
- 忘记打的是上班卡还是下班卡

提交接口：

```http
POST /api/missing-punches
```

请求示例：

```json
{
  "work_date": "2026-07-24",
  "punch_type": "ON_DUTY"
}
```

后端创建一条 `auto_block_missing_punches` 记录，初始状态为：

```text
PENDING
```

---

### 3.3 补卡聊天

页面：

```text
frontend/src/views/RepairChat.vue
```

页面分为两部分：

- 左侧：聊天记录列表
- 右侧：当前聊天内容和补卡流程信息

右侧顶部展示：

1. 本次会话针对的补卡数据
2. 当前流程节点
3. 本轮补卡结果
4. 聊天历史内容
5. 输入框

如果当前会话已经结束，输入框会被禁用，用户必须点击“新会话”才能开启下一轮。

---

## 4. 补卡聊天主流程

补卡聊天统一使用接口：

```http
POST /api/agent/chat
```

请求示例：

```json
{
  "message": "我要补卡",
  "session_id": null
}
```

如果继续同一轮聊天：

```json
{
  "message": "选第1条",
  "session_id": 10
}
```

后端响应是 SSE 流式输出：

```http
Content-Type: text/event-stream
```

---

## 5. OpenAI 意图理解流程

核心文件：

```text
backend/app/services/agent.py
```

核心类：

```python
AutoClockAgent
```

Agent 使用 OpenAI 理解用户自然语言。

配置：

```python
ChatOpenAI(
    model=settings.openai_model,
    api_key=settings.openai_api_key,
    base_url=settings.openai_base_url or None,
    temperature=0,
)
```

### 5.1 支持的用户意图

OpenAI 会把用户输入解析成以下三类意图：

| intent | 说明 |
|---|---|
| `list_records` | 用户想补卡，但还没选择具体记录 |
| `select_record` | 用户已经选择了某条补卡记录 |
| `other` | 用户意图不明确或和补卡无关 |

---

### 5.2 用户说“我要补卡”

例如用户输入：

```text
我要补卡
```

或：

```text
我忘记打卡了
```

OpenAI 理解为：

```json
{
  "intent": "list_records",
  "selected_id": null,
  "reply": null
}
```

然后 Agent 调用内部工具查询当前用户所有可补卡数据。

查询函数：

```python
list_my_missing_punches(db, user, only_open=True)
```

只查询：

```text
PENDING
APPROVAL_PENDING
```

然后在聊天框中展示：

```text
我调用工具查询到了你当前所有可补卡的数据，请回复编号或第几条来选择：
1. 编号 12｜2026-07-24｜上班卡｜状态 PENDING
2. 编号 13｜2026-07-23｜下班卡｜状态 PENDING

例如：回复“选第 1 条”或“补编号 12”。
```

此时流程节点会显示：

```text
查询可补卡数据
```

随后变为：

```text
等待用户选择补卡数据
```

---

### 5.3 用户选择某条数据

用户可以输入：

```text
选第1条
```

或：

```text
补编号12
```

OpenAI 理解为：

```json
{
  "intent": "select_record",
  "selected_id": 12,
  "reply": null
}
```

后端会立刻把当前聊天会话绑定到该补卡记录：

```text
auto_block_chat_sessions.missing_punch_id = 12
```

同时更新聊天标题，例如：

```text
2026-07-24 上班卡补卡
```

前端右侧顶部“本次会话针对的补卡数据”会显示：

```text
编号 #12
2026-07-24
上班卡
PENDING
```

---

## 6. LangGraph 补卡流程

当用户选择具体补卡记录后，会进入 LangGraph 流程。

核心图节点：

```text
load_selected_record
check_leader_policy
auto_repair
request_leader_approval
end
```

流程图：

```mermaid
flowchart TD
    A["load_selected_record\n加载用户选择的补卡记录"] --> B["check_leader_policy\n查询领导与审核配置"]
    B -->|"领导开启自动审核"| C["auto_repair\n自动补卡"]
    B -->|"领导未开启自动审核"| D["request_leader_approval\n提交领导审核"]
    C --> E["end\n流程结束"]
    D --> E["end\n流程结束"]
```

---

### 6.1 节点：`load_selected_record`

作用：

- 根据用户选择的 `missing_punch_id` 查询补卡记录。
- 校验这条记录属于当前登录用户。
- 校验状态是否允许处理。

查询条件：

```text
MissingPunch.id == selected_id
MissingPunch.user_id == current_user.id
MissingPunch.status in (PENDING, APPROVAL_PENDING)
```

如果找不到记录，说明：

- 用户选错了
- 记录已经补卡
- 记录已经被拒绝
- 记录不属于当前用户

此时 Agent 回复失败原因，并进入 `end`。

---

### 6.2 节点：`check_leader_policy`

作用：

- 查询当前用户的领导。
- 查询领导是否开启自动审核打卡。

逻辑：

1. 如果当前用户没有配置领导，当前实现按无领导场景直接自动补卡。
2. 如果找不到有效领导，也直接自动补卡。
3. 如果领导存在，判断领导字段：

```text
attendance_auto_audit_enabled
```

如果为 `true`：

```text
进入 auto_repair 节点
```

如果为 `false`：

```text
进入 request_leader_approval 节点
```

---

### 6.3 节点：`auto_repair`

作用：自动补卡。

调用函数：

```python
repair_missing_punch(db, missing, operator_id, message)
```

具体处理：

1. 把补卡记录状态改为：

```text
REPAIRED
```

2. 设置补卡完成时间：

```text
repaired_at = 当前时间
```

3. 写入补卡日志：

```text
auto_block_repair_logs.action = REPAIR
```

4. Agent 输出：

```text
自动补卡完成
```

5. 会话状态更新为：

```text
DONE
```

6. 流程节点更新为：

```text
end
```

---

### 6.4 节点：`request_leader_approval`

作用：提交领导审核。

调用函数：

```python
ensure_pending_approval(db, missing, applicant, leader)
```

具体处理：

1. 检查当前补卡记录是否已经存在待审核单。
2. 如果已有待审核单，复用原审核单。
3. 如果没有，则创建 `auto_block_repair_approvals` 记录。
4. 把补卡记录状态改为：

```text
APPROVAL_PENDING
```

5. 写入补卡日志：

```text
auto_block_repair_logs.action = REQUEST_APPROVAL
```

6. Agent 输出：

```text
已提交领导审核，审核单号：xx。领导通过后系统会自动补卡。
```

7. 会话状态更新为：

```text
WAITING_APPROVAL
```

8. 流程节点更新为：

```text
end
```

注意：

- 此时本轮 Agent 对话已经结束。
- 但补卡最终结果还要等领导审核。
- 前端会禁用当前会话输入框。

---

## 7. 领导审核流程

领导审核页面：

```text
frontend/src/views/Approvals.vue
```

查询待审核单：

```http
GET /api/approvals
```

提交审核结果：

```http
POST /api/approvals/{approval_id}/review
```

请求示例：

```json
{
  "approved": true,
  "comment": "同意"
}
```

---

### 7.1 领导审核通过

后端调用：

```python
review_approval(db, approval, reviewer, approved=True, comment=...)
```

具体处理：

1. 审核单状态改为：

```text
APPROVED
```

2. 设置审核时间：

```text
reviewed_at = 当前时间
```

3. 调用自动补卡函数：

```python
repair_missing_punch(db, missing, reviewer.id, "领导审核通过后自动补卡")
```

4. 补卡记录状态改为：

```text
REPAIRED
```

5. 写入补卡日志：

```text
REPAIR
```

6. 同步关联聊天会话：

```text
auto_block_chat_sessions.status = DONE
auto_block_chat_sessions.current_node = end
```

---

### 7.2 领导审核拒绝

如果领导拒绝：

1. 审核单状态改为：

```text
REJECTED
```

2. 补卡记录状态改为：

```text
REJECTED
```

3. 写入补卡日志：

```text
REJECT
```

4. 同步关联聊天会话：

```text
auto_block_chat_sessions.status = FAILED
auto_block_chat_sessions.current_node = end
```

---

## 8. 聊天记录与历史回看

### 8.1 聊天列表

接口：

```http
GET /api/chat-sessions
```

返回当前用户的聊天会话列表，包含：

- 会话 ID
- 标题
- 状态
- 当前节点
- 消息数量
- 最后一条消息
- 本会话对应的补卡记录 ID
- 补卡日期
- 上班卡 / 下班卡
- 补卡记录状态

左侧聊天列表会展示：

```text
#10 2026-07-24 上班卡补卡
已完成 · 流程结束 · 4 条
补卡 #12 ｜ 2026-07-24 ｜ 上班卡
最后一条消息摘要
```

---

### 8.2 聊天详情

接口：

```http
GET /api/chat-sessions/{session_id}
```

返回：

- 会话信息
- 本次会话针对的补卡数据
- 全部聊天消息

点击左侧聊天记录后，右侧会显示：

1. 本次会话针对的补卡数据
2. 当前流程节点
3. 本轮补卡结果
4. 完整聊天历史记录

---

### 8.3 历史数据兼容

早期会话可能没有绑定：

```text
missing_punch_id
```

但聊天内容中可能保存了：

```text
审核单号：xx
```

当前后端会在查询聊天列表或详情时做兜底处理：

1. 从聊天消息中解析审核单号。
2. 通过审核单号查询审核单。
3. 找到对应的 `missing_punch_id`。
4. 自动补齐聊天会话的 `missing_punch_id`。
5. 根据补卡记录状态修正聊天会话状态。

---

## 9. SSE 流式输出

后端使用：

```python
StreamingResponse(gen(), media_type="text/event-stream")
```

每条事件格式：

```text
event: message
data: {"type":"text","content":"..."}
```

常见事件类型：

### 9.1 会话事件

```json
{
  "type": "session",
  "session_id": 10,
  "title": "补卡会话"
}
```

前端收到后绑定当前会话 ID。

---

### 9.2 节点事件

```json
{
  "type": "node",
  "node": "check_leader_policy",
  "session_id": 10
}
```

前端收到后更新“当前流程节点”。

---

### 9.3 文本事件

```json
{
  "type": "text",
  "content": "你的领导是张三，需要先提交领导审核。",
  "session_id": 10
}
```

前端收到后追加到聊天窗口。

---

### 9.4 结束事件

流程结束时返回：

```json
{
  "type": "done",
  "session_id": 10,
  "status": "DONE",
  "current_node": "end",
  "terminal": true,
  "result_status": "SUCCESS",
  "result_message": "补卡成功，当前记录已自动补卡完成。",
  "repair_record": {
    "id": 12,
    "work_date": "2026-07-24",
    "punch_type": "ON_DUTY",
    "punch_type_label": "上班卡",
    "status": "REPAIRED"
  }
}
```

前端据此展示“本轮补卡结果”。

---

## 10. 前端会话结束控制

当满足以下条件之一：

```text
current_node == end
status in (DONE, WAITING_APPROVAL, FAILED)
```

前端认为本轮对话已结束。

此时：

- 输入框禁用
- 发送按钮禁用
- 页面提示“本轮对话已结束，请新建会话”
- 用户必须点击“新会话”才能继续新的补卡流程

后端也做了兜底控制：

如果用户绕过前端，继续向已结束会话发送消息，后端会返回提示并拒绝继续执行 Agent 流程。

---

## 11. 本轮补卡结果展示

右侧“本轮补卡结果”会根据会话状态展示。

### 11.1 成功

条件：

```text
status = DONE
```

展示：

```text
补卡成功
补卡记录编号：#12
补卡日期：2026-07-24
补卡类型：上班卡
记录状态：REPAIRED
补卡成功，当前记录已自动补卡完成。
```

---

### 11.2 等待领导审核

条件：

```text
status = WAITING_APPROVAL
```

展示：

```text
等待领导审核
补卡记录编号：#12
补卡日期：2026-07-24
补卡类型：下班卡
记录状态：APPROVAL_PENDING
已提交领导审核，领导通过后会自动补卡。
```

---

### 11.3 失败

条件：

```text
status = FAILED
```

展示：

```text
补卡失败
补卡记录编号：#12
补卡日期：2026-07-24
补卡类型：上班卡
记录状态：REJECTED
补卡失败，请查看聊天内容中的失败原因。
```

---

## 12. 当前“自动补卡”的边界

当前系统中的“自动补卡”是内部业务状态流转。

也就是说，自动补卡目前做的是：

```text
auto_block_missing_punches.status = REPAIRED
```

并记录：

```text
repaired_at
auto_block_repair_logs
```

目前还没有接入真实公司考勤系统 API。

如果后续需要真正调用第三方考勤系统，可以把集成逻辑放在：

```text
backend/app/services/attendance_service.py
```

函数：

```python
repair_missing_punch(...)
```

建议在该函数中增加：

1. 调用真实考勤系统补卡 API。
2. 根据 API 结果决定是否更新为 `REPAIRED`。
3. 如果 API 调用失败，记录失败原因并返回失败状态。

---

## 13. 总结

当前自动补卡流程可以概括为：

1. 员工先录入忘记打卡数据。
2. 员工进入补卡聊天页面，用自然语言告诉 Agent 要补卡。
3. OpenAI 理解用户意图。
4. Agent 调用工具查询当前用户可补卡数据。
5. 用户通过聊天选择某条补卡数据。
6. 系统绑定聊天会话和补卡记录。
7. LangGraph 进入补卡流程。
8. 系统查询用户领导及领导审核配置。
9. 如果领导开启自动审核，直接自动补卡。
10. 如果领导未开启自动审核，提交领导审核。
11. 领导通过后自动补卡；领导拒绝则补卡失败。
12. 聊天会话、聊天消息、流程节点、补卡结果都会保存。
13. 用户可以点击历史聊天记录，查看完整聊天内容和该会话对应的补卡数据。
