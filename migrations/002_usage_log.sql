-- Phase 2:用量紀錄
--
-- 兩個用途:
-- 1. 追蹤 OpenAI 成本(哪種用途、花了多少 token)
-- 2. 統計 LINE push 訊息的月用量 —— 這才是急迫的那個。
--    專案採輕用量方案,每月只有 200 則免費 push,超過要付費。
--    reply 免費不計入,所以「讓 reply 成功」是成本控制的一環(見 docs/DESIGN.md 5.1)。

create table if not exists usage_log (
    id                 uuid primary key default gen_random_uuid(),
    line_user_id       text,
    -- chat: 對話回覆 / extract: 記憶抽取 / push: 主動推送 / notify: 給擁有者的通知
    kind               text        not null check (kind in ('chat', 'extract', 'push', 'notify')),
    model              text,
    prompt_tokens      integer     not null default 0,
    completion_tokens  integer     not null default 0,
    created_at         timestamptz not null default now()
);

-- 每次要送 push 前都會查「這個月已經送幾則」,這個索引讓它只掃當月的列
create index if not exists usage_log_kind_created_idx
    on usage_log (kind, created_at desc);

alter table usage_log enable row level security;

comment on table usage_log is 'OpenAI token 與 LINE push 的用量紀錄,push 用於月額度控管';
