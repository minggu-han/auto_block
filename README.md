# Auto Clock Agent

## 快速启动

### 后端

建议使用 Python 3.13 或 3.12。`requirements.txt` 已使用兼容 Python 3.13 的 LangChain/LangGraph/Numpy 版本，避免 Windows 下 numpy 1.26.x 无 wheel 而尝试本地编译。

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env
# 编辑 .env，填写 OPENAI_API_KEY；如使用兼容 OpenAI 的网关，可填写 OPENAI_BASE_URL
uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
```

### 前端

```powershell
cd frontend
npm install
npm run dev
```

前端默认代理 `/api` 到 `http://localhost:8001`。

## 数据库

手动执行：

```sql
\i ddl/001_init.sql
```

默认连接串：

```text
postgresql+asyncpg://mcp:mcp@172.18.1.76:5432/mcp
```

