-- =====================================================================
-- Kestrel Invoice Bench — соответствие писем эталонным документам
--
-- Накатывать после 06-score.sql.
--
-- Зачем нужна отдельная таблица, а не score.doc_map:
-- doc_map ссылается на doc.documents, а эта строка появляется только
-- когда конвейер примет письмо. На момент укладки писем её ещё нет.
-- Здесь связь держится за отпечаток файла — он известен сразу и не
-- меняется. После приёма doc_map строится соединением по file_sha256.
--
-- Конвейер к схеме score доступа не имеет, поэтому подсмотреть
-- соответствие из неё нельзя.
-- =====================================================================

create table score.mail_map (
    file_sha256      text primary key,
    truth_doc_id     text not null references truth.documents,
    attachment_name  text not null,
    message_id       text,
    from_email       text,
    subject          text,
    sent_at          timestamptz,
    created_at       timestamptz not null default now()
);

comment on table score.mail_map is
    'Какое письмо какому эталонному документу отвечает. Пишется скриптом '
    'укладки писем, читается только подсчётом метрик.';

comment on column score.mail_map.attachment_name is
    'Имя файла во вложении — без префикса INV/DUP. Префикс убран намеренно: '
    'в нём написан номер документа из журнала, и по нему дубль опознавался бы '
    'по имени файла вместо разбора счёта.';

grant select, insert, update, delete on score.mail_map to bench_score;
