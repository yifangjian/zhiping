-- Phase 3 修正:讓「忘記」是真的忘記
--
-- 原本刪除只作用在 memories,但原始對話還留在最近 20 則的短期脈絡裡,
-- 知平照樣講得出使用者要求忘記的內容。使用者說「連寫出來都不該」——他是對的,
-- 刪掉記憶不等於忘記。
--
-- 作法是遮蔽而非刪除:規格 3.4 要求對話紀錄保留(方便追溯與復原),
-- 所以只是不再送進脈絡。

alter table conversations
    add column if not exists hidden boolean not null default false;

comment on column conversations.hidden is
    '使用者要求忘記後遮蔽,不再進入短期脈絡。列本身保留,可追溯與復原';

-- 撈脈絡時會多一個 hidden = false 的條件,索引跟著改成 partial index
create index if not exists conversations_user_visible_idx
    on conversations (line_user_id, created_at desc)
    where hidden = false;
