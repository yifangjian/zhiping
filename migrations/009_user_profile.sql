-- 使用者的背景描述
--
-- 原本「他是輪機系實習生、在遠洋商船上工作」是寫死在 prompts.py 裡的。
-- 那有兩個問題:
--   1. 別人拿這個專案去用,得改程式碼才能換掉背景
--   2. 他的處境會變(實習結束、換船、畢業),寫死的那句會永遠停在原地
--
-- 搬進資料庫之後,背景可以跟著對話更新(見 app/services/memory_extraction.py)。
--
-- **背景與記憶的差別**:記憶是一條一條的事實(「他在學排氣閥」),
-- 背景是理解那些事實所需的框架(「他是輪機系實習生」)。前者常變、後者少變,
-- 混在一起的話,幾十條記憶會把框架淹沒。

alter table user_state
    add column if not exists profile text,
    add column if not exists profile_updated_at timestamptz;

comment on column user_state.profile is
    '使用者的背景描述,會進 system prompt。只寫事實,不寫行為規則';

-- 背景異動紀錄。它會影響之後每一次對話,改錯了要查得出來是什麼時候、改成什麼
create table if not exists profile_history (
    id            uuid primary key default gen_random_uuid(),
    line_user_id  text        not null,
    before_text   text,
    after_text    text        not null,
    -- extract(抽取流程自動更新)/ owner(專案擁有者手動設定)
    actor         text        not null default 'extract'
        check (actor in ('extract', 'owner')),
    created_at    timestamptz not null default now()
);

create index if not exists profile_history_created_idx
    on profile_history (line_user_id, created_at desc);

alter table profile_history enable row level security;

comment on table profile_history is '背景描述的異動紀錄,用於追溯自動更新的品質';
