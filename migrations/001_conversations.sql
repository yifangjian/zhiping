-- Phase 1:對話紀錄
--
-- 在 Supabase 的 SQL Editor 貼上執行即可。可重複執行(全部用 if not exists)。

create extension if not exists "pgcrypto";  -- gen_random_uuid()

create table if not exists conversations (
    id                uuid primary key default gen_random_uuid(),
    line_user_id      text        not null,
    -- LINE webhook 事件 ID。重送機制開啟後同一事件可能送達多次,
    -- 靠下面的 unique index 在資料庫層擋掉(見 docs/DESIGN.md 5.4)。
    -- 知平自己的回覆沒有對應的 webhook 事件,所以允許 null。
    webhook_event_id  text,
    role              text        not null check (role in ('user', 'assistant')),
    content           text        not null,
    created_at        timestamptz not null default now(),
    -- 記憶抽取流程是否已處理過這則對話(Phase 3)
    extracted         boolean     not null default false
);

-- 去重的關鍵。用 partial index:只對有值的列建立限制,
-- 讓 assistant 的 null 不受影響,索引也比較小。
create unique index if not exists conversations_webhook_event_id_key
    on conversations (webhook_event_id)
    where webhook_event_id is not null;

-- 每次回覆前都要撈最近 N 則對話(Phase 3 的短期脈絡),這個索引讓它只掃需要的列
create index if not exists conversations_user_created_idx
    on conversations (line_user_id, created_at desc);

-- 記憶抽取只看還沒處理過的對話(Phase 3)
create index if not exists conversations_unextracted_idx
    on conversations (line_user_id, created_at)
    where extracted = false;

-- 後端一律使用 service role key(會繞過 RLS)。這裡開啟 RLS 但不建立任何 policy,
-- 等於「除了 service role 以外誰都讀不到」——萬一 anon key 外流,對話紀錄仍是安全的。
alter table conversations enable row level security;

comment on table conversations is '知平的對話逐字紀錄,兼作 webhook 事件去重的依據';
