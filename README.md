# Auto Clock Agent

自动补卡 Agent：Vue3 前端 + Python FastAPI 后端 + LangChain/LangGraph + PostgreSQL。

## 功能

- 登录/当前用户信息
- 用户录入忘记打卡数据（日期 + 上班卡/下班卡，不需要填写原因）
- 聊天式补卡页面，SSE 流式输出；使用 OpenAI 理解用户意图，通过工具查询可补卡数据并在聊天框中展示
- LangGraph 工作流：
  1. 查询当前用户所有待补卡数据并让用户选择
  2. 查询用户领导及领导是否开启打卡审核
  3. 若领导开启“免审核/自动审核打卡”，直接自动补卡
  4. 若未开启，则创建审核单，等待领导审核后补卡
- 领导审核页面
- DDL 脚本在 `ddl/001_init.sql`，项目启动不会自动建表

## 快速启动

### 后端\n\n建议使用 Python 3.13 或 3.12。`requirements.txt` 已使用兼容 Python 3.13 的 LangChain/LangGraph/Numpy 版本，避免 Windows 下 numpy 1.26.x 无 wheel 而尝试本地编译。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
# 编辑 .env，填写 OPENAI_API_KEY；如使用兼容 OpenAI 的网关，可填写 OPENAI_BASE_URL
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 前端

```powershell
cd frontend
npm install
npm run dev
```

前端默认代理 `/api` 到 `http://localhost:8000`。

## 数据库

手动执行：

```sql
\i ddl/001_init.sql
```

默认连接串：

```text
postgresql+asyncpg://mcp:mcp@172.18.1.76:5432/mcp
```

