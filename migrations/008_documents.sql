-- 使用者傳來的檔案內容
--
-- 原本只用行程內快取記 30 分鐘,但那只是把「立刻忘記」變成「晚一點忘記」,
-- 而且伺服器重啟就沒了。他寫的作業、船上的手冊,下週想再問一次是很自然的事。
--
-- 存的是**抽出來的文字**,不是原始檔案:
--   1. 需要的是文字。原始的 docx 二進位對後續對話沒有用處
--   2. 少存一份原始檔就少一個外洩的可能——那可能是他的作業或私人書寫
--   3. 不用額外開 storage bucket,備份腳本也能一併帶走

create table if not exists documents (
    id                      uuid primary key default gen_random_uuid(),
    line_user_id            text        not null,
    file_name               text        not null,
    -- 抽出的純文字。上限由應用層控制(見 app/services/attachments.py)
    content                 text        not null,
    char_count              integer     not null default 0,
    source_conversation_id  uuid        references conversations (id) on delete set null,
    created_at              timestamptz not null default now(),
    -- 軟刪除。與 memories 一致:使用者說忘記就設 false,不做實體刪除
    is_active               boolean     not null default true
);

-- 找「他最近傳的檔案」用
create index if not exists documents_recent_idx
    on documents (line_user_id, created_at desc)
    where is_active = true;

alter table documents enable row level security;

comment on table documents is
    '使用者傳來的檔案抽取後的文字。軟刪除,使用者可查看與刪除';
