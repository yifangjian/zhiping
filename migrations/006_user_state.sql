-- Phase 6:使用者狀態(時區)
--
-- 使用者在遠洋船上跨時區移動(台灣、中國、越南、日本、韓國循環)。
-- 若知平用固定時區判斷「早上」「晚上」會出錯——他半夜下班說「累死了」
-- 得到一句「早安啊」,那種細節會讓陪伴感整個垮掉。

create table if not exists user_state (
    line_user_id        text primary key,
    -- IANA 時區字串,例如 Asia/Taipei、Asia/Ho_Chi_Minh
    timezone            text,
    -- 最後一次更新時區的時間。用來判斷這個資訊有多舊,
    -- 也方便事後檢查 AI 有沒有亂改
    timezone_updated_at timestamptz,
    updated_at          timestamptz not null default now()
);

alter table user_state enable row level security;

comment on table user_state is '使用者狀態。目前只有時區,因為他在船上跨時區移動';
