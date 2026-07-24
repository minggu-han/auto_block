-- 已经执行过 001_init.sql 的环境，请单独执行本增量脚本。

CREATE TABLE IF NOT EXISTS auto_block_chat_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    missing_punch_id INTEGER NULL REFERENCES auto_block_missing_punches(id),
    title VARCHAR(255) NOT NULL DEFAULT '补卡会话',
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    current_node VARCHAR(64) NOT NULL DEFAULT 'start',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE auto_block_chat_sessions IS '补卡 Agent 聊天会话记录';

CREATE TABLE IF NOT EXISTS auto_block_chat_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES auto_block_chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE auto_block_chat_messages IS '补卡 Agent 聊天消息明细';

CREATE INDEX IF NOT EXISTS idx_auto_block_chat_sessions_user_updated ON auto_block_chat_sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_auto_block_chat_sessions_missing ON auto_block_chat_sessions(missing_punch_id);
CREATE INDEX IF NOT EXISTS idx_auto_block_chat_messages_session_id ON auto_block_chat_messages(session_id, id);

