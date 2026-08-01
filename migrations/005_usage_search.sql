-- Phase 4:把 search 加進 usage_log 的用途分類
--
-- 一次 web search 的 input token 約是一般對話的 40 倍(搜尋結果會整批進 context),
-- 所以「這個月查了幾次」是成本控制上必須看得到的數字。

alter table usage_log drop constraint if exists usage_log_kind_check;

alter table usage_log add constraint usage_log_kind_check
    check (kind in ('chat', 'extract', 'push', 'notify', 'search'));

comment on column usage_log.kind is
    'chat 對話 / extract 記憶抽取 / push 主動推送 / notify 擁有者通知 / search 網路查詢';
