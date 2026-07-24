-- 已经执行过聊天记录表的环境，请单独执行本增量脚本。
-- 用于在聊天页面展示当前 LangGraph/Agent 流程节点，并判断本轮对话是否结束。

ALTER TABLE auto_block_chat_sessions
    ADD COLUMN IF NOT EXISTS current_node VARCHAR(64) NOT NULL DEFAULT 'start';

UPDATE auto_block_chat_sessions
SET current_node = 'end'
WHERE status IN ('DONE', 'WAITING_APPROVAL')
  AND (current_node IS NULL OR current_node = 'start');
