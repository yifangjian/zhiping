-- Phase 3:長期記憶
--
-- 長期記憶與短期脈絡是兩件事,不要混為一談(見 docs/DESIGN.md 3.5):
--   memories      抽取後的事實,例如「他在學排氣閥」——讓知平知道他是誰、在意什麼
--   conversations 原始逐字對話——讓知平聽得懂「那個」「剛剛講的」指什麼

create table if not exists memories (
    id                      uuid primary key default gen_random_uuid(),
    line_user_id            text        not null,
    -- preference   喜好、討厭的事物、習慣
    -- context      目前處境(工作狀況、正在煩惱的事)
    -- relationship 人際關係中的重要人物
    -- event        具時間性的事件
    category                text        not null
        check (category in ('preference', 'context', 'relationship', 'event')),
    content                 text        not null,
    -- 1–5,影響是否被納入 system prompt
    importance              integer     not null default 3
        check (importance between 1 and 5),
    -- 來源對話,方便事後追溯這條記憶是從哪句話來的
    source_conversation_id  uuid        references conversations (id) on delete set null,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now(),
    -- 軟刪除。使用者說「忘記」時只是設成 false,不做實體刪除,方便追溯與復原
    is_active               boolean     not null default true
);

-- 每次回覆前都要撈記憶(時間預算 < 500ms),排序方式固定,所以索引直接照著建
create index if not exists memories_active_priority_idx
    on memories (line_user_id, importance desc, updated_at desc)
    where is_active = true;

-- 「忘記 <關鍵字>」要做內容比對
create index if not exists memories_content_idx
    on memories (line_user_id, content);

alter table memories enable row level security;

comment on table memories is '從對話中抽取的長期記憶,軟刪除,使用者可查看與刪除';


-- 記憶異動紀錄
--
-- 規格列為選配,但值得做:AI 抽取的品質需要人工抽查,而記憶是軟刪除的,
-- 沒有這張表就看不出「這條記憶何時被改成現在這樣」。
create table if not exists memory_audit (
    id            uuid primary key default gen_random_uuid(),
    memory_id     uuid,
    line_user_id  text        not null,
    action        text        not null check (action in ('create', 'update', 'deactivate')),
    -- 異動前後的內容,方便直接比對 AI 改了什麼
    before_content text,
    after_content  text,
    -- 誰做的:extract(抽取流程)/ user(使用者指令)
    actor         text        not null default 'extract' check (actor in ('extract', 'user')),
    created_at    timestamptz not null default now()
);

create index if not exists memory_audit_created_idx
    on memory_audit (line_user_id, created_at desc);

alter table memory_audit enable row level security;

comment on table memory_audit is '記憶的新增/修改/停用紀錄,用於檢查 AI 抽取品質';
