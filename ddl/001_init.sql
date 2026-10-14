-- Auto Clock Agent 初始化脚本
-- 注意：项目启动不会自动创建表，请手动执行本脚本。
-- 表前缀：auto_block_

CREATE TYPE auto_block_punch_type AS ENUM ('ON_DUTY', 'OFF_DUTY');
CREATE TYPE auto_block_missing_punch_status AS ENUM ('PENDING', 'APPROVAL_PENDING', 'REPAIRED', 'REJECTED');
CREATE TYPE auto_block_approval_status AS ENUM ('PENDING', 'APPROVED', 'REJECTED');

CREATE TABLE auto_block_users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    full_name VARCHAR(128) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    leader_id INTEGER NULL REFERENCES auto_block_users(id),
    attendance_auto_audit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON COLUMN auto_block_users.attendance_auto_audit_enabled IS '作为领导时是否开启自动审核/免审核打卡；开启后下属补卡无需人工审核';

CREATE TABLE auto_block_missing_punches (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    work_date DATE NOT NULL,
    punch_type auto_block_punch_type NOT NULL,
    reason TEXT NULL,
    status auto_block_missing_punch_status NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    repaired_at TIMESTAMPTZ NULL
);

CREATE TABLE auto_block_repair_approvals (
    id SERIAL PRIMARY KEY,
    missing_punch_id INTEGER NOT NULL REFERENCES auto_block_missing_punches(id),
    applicant_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    leader_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    status auto_block_approval_status NOT NULL DEFAULT 'PENDING',
    comment TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ NULL
);

CREATE TABLE auto_block_repair_logs (
    id SERIAL PRIMARY KEY,
    missing_punch_id INTEGER NOT NULL REFERENCES auto_block_missing_punches(id),
    operator_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    action VARCHAR(64) NOT NULL,
    message TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_auto_block_users_leader_id ON auto_block_users(leader_id);
CREATE INDEX idx_auto_block_missing_user_status ON auto_block_missing_punches(user_id, status);
CREATE INDEX idx_auto_block_missing_work_date ON auto_block_missing_punches(work_date);
CREATE INDEX idx_auto_block_approval_leader_status ON auto_block_repair_approvals(leader_id, status);
CREATE INDEX idx_auto_block_approval_missing ON auto_block_repair_approvals(missing_punch_id);

-- 演示账号：密码均为 123456。
-- 生产环境请使用 backend/app/core/security.py 的 get_password_hash 生成 bcrypt 密码并替换 plain: 明文开发密码。
INSERT INTO auto_block_users (id, username, full_name, password_hash, leader_id, attendance_auto_audit_enabled) VALUES
(1, 'leader_auto', '自动审核领导', 'plain:123456', NULL, TRUE),
(2, 'leader_manual', '人工审核领导', 'plain:123456', NULL, FALSE),
(3, 'alice', '张三', 'plain:123456', 1, FALSE),
(4, 'bob', '李四', 'plain:123456', 2, FALSE);
SELECT setval('auto_block_users_id_seq', (SELECT max(id) FROM auto_block_users));

CREATE TABLE auto_block_chat_sessions (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES auto_block_users(id),
    missing_punch_id INTEGER NULL REFERENCES auto_block_missing_punches(id),
    title VARCHAR(255) NOT NULL DEFAULT '补卡会话',
    status VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    current_node VARCHAR(64) NOT NULL DEFAULT 'start',
    summary TEXT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE auto_block_chat_sessions IS '补卡 Agent 聊天会话记录';
COMMENT ON COLUMN auto_block_chat_sessions.summary IS '滑动窗口之外的旧消息摘要，用于控制上下文长度';

CREATE TABLE auto_block_chat_messages (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES auto_block_chat_sessions(id) ON DELETE CASCADE,
    role VARCHAR(32) NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE auto_block_chat_messages IS '补卡 Agent 聊天消息明细';

CREATE INDEX idx_auto_block_chat_sessions_user_updated ON auto_block_chat_sessions(user_id, updated_at DESC);
CREATE INDEX idx_auto_block_chat_sessions_missing ON auto_block_chat_sessions(missing_punch_id);
CREATE INDEX idx_auto_block_chat_messages_session_id ON auto_block_chat_messages(session_id, id);

