-- Phase 7:把 backup 加進 usage_log 的用途分類
--
-- 備份腳本每跑一次就記一筆。健康檢查靠它判斷「多久沒備份了」——
-- 規格 8.3 要求超過 30 天未備份時提醒擁有者。
--
-- 為什麼記在資料庫而不是看備份資料夾:備份檔在擁有者的電腦上,
-- 健康檢查跑在伺服器上,兩邊看不到彼此的檔案系統。

alter table usage_log drop constraint if exists usage_log_kind_check;

alter table usage_log add constraint usage_log_kind_check
    check (kind in ('chat', 'extract', 'push', 'notify', 'search', 'backup'));

comment on column usage_log.kind is
    'chat 對話 / extract 記憶抽取 / push 主動推送 / notify 擁有者通知 / '
    'search 網路查詢 / backup 資料備份';
