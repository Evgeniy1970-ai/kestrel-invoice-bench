-- =====================================================================
-- Kestrel Invoice Bench — 09-mails.sql
-- Входящие письма: след каждого письма, включая те, из которых
-- не вышло ни одного документа.
--
-- Накатывать ПОСЛЕ 03-doc.sql: столбец doc.documents.mail_id
-- ссылается на таблицу, которая создаётся здесь.
--
-- Зачем таблица. WF-01 разбирает письмо на PDF-вложения. Письмо без
-- вложений и письмо, где вложения есть, но среди них нет PDF, до
-- 30 августа исчезали бесследно: цикл по вложениям не выполнялся ни
-- разу, узел возвращал пустой список, ошибки не было. В измерении
-- такое письмо нельзя ни поймать, ни пропустить — его просто нет.
--
-- Пишутся ВСЕ письма, а не только проблемные. Это даёт сквозную
-- сверку: пришло N писем, извлечено M документов, разница объяснима
-- поимённо.
--
-- Письма живут вне прогонов, как и документы: run_id здесь нет.
-- Прогон принадлежит извлечению и сверке.
-- =====================================================================

create table if not exists doc.mails (
    id                bigserial primary key,

    -- Заголовок Message-ID. Уникален по стандарту, но его может не быть
    -- вовсе, поэтому UNIQUE с допуском пустоты, а не первичный ключ.
    -- Защищает от дубля при повторном запуске WF-01.
    message_id        text unique,

    sender            text,
    subject           text,

    -- Дата из заголовка Date письма, а не время получения нами.
    received_at       timestamptz,

    -- Есть ли отправитель в ref.suppliers со статусом approved.
    -- Отдельным полем, а не внутри status: это признак отправителя,
    -- а не письма. Письмо от постороннего без вложений — самый мутный
    -- случай, и по одному только status его не опознать.
    --
    -- Считается ОДИН РАЗ НА ПИСЬМО, до перебора вложений. Если считать
    -- внутри цикла по PDF, то у письма без PDF поле останется пустым,
    -- а пустота в SQL означает «неизвестно», не «нет»: запрос
    -- where sender_approved = false такое письмо молча пропустит.
    sender_approved   boolean,

    attachment_count  int not null default 0,
    pdf_count         int not null default 0,

    -- Имена и MIME-типы вложений, отброшенных как не-PDF.
    -- Одной строкой: bill.docx (application/msword); logo.png (image/png)
    -- Не массив и не JSON намеренно — поле только для чтения человеком,
    -- решающим, открывать ли письмо руками.
    --
    -- Автоматика на него не опирается и опираться не должна: имя файла
    -- ничем не гарантировано. bill.docx, Rechnung_04.docx и
    -- document (2).pdf для правил неразличимы, а в проекте действует
    -- правило «никакой сигнал, кроме самого признака, не должен
    -- предсказывать ответ». MIME-тип надёжнее имени, но и он не говорит,
    -- счёт ли внутри.
    skipped_files     text,

    --   ingested       — из письма вышел хотя бы один PDF
    --   no_attachment  — вложений нет вовсе
    --   no_pdf         — вложения есть, PDF среди них нет
    status            text not null,
    constraint mails_status_check
        check (status in ('ingested', 'no_attachment', 'no_pdf')),

    created_at        timestamptz not null default now()
);

comment on table doc.mails is
    'Входящие письма WF-01. Пишутся все, включая те, из которых не вышло документов.';

comment on column doc.mails.skipped_files is
    'Имена и MIME-типы вложений, отброшенных как не-PDF. Только для чтения человеком: имя файла ничего не гарантирует, автоматика на это поле не опирается.';

create index if not exists mails_status_idx    on doc.mails (status);
create index if not exists mails_sender_idx    on doc.mails (lower(sender));
create index if not exists mails_received_idx  on doc.mails (received_at);

-- ---------------------------------------------------------------------
-- Связь письма с документами.
--
-- Одно письмо даёт ноль, один или несколько документов, поэтому ссылка
-- живёт в documents, а не в mails.
--
-- Пустоту допускает: у девяти документов, принятых до 30 августа,
-- письма не записывались. Заполнять их задним числом нечем и незачем.
--
-- on delete set null: удаление письма не должно уносить документ.
-- Файл принят, он существует, происхождение — второстепенно.
-- ---------------------------------------------------------------------

alter table doc.documents
    add column if not exists mail_id bigint;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'documents_mail_id_fkey'
    ) then
        alter table doc.documents
            add constraint documents_mail_id_fkey
            foreign key (mail_id) references doc.mails (id)
            on delete set null;
    end if;
end $$;

create index if not exists documents_mail_id_idx on doc.documents (mail_id);

-- ---------------------------------------------------------------------
-- Функция записи письма.
--
-- Зачем функция, а не два отдельных INSERT в узлах n8n. Письмо
-- записывается с двух веток WF-01: с ветки без PDF (узел Save mail) и
-- с ветки с PDF (узел Save document, который заодно получает mail_id
-- для вставки документа). Два отдельных INSERT рано или поздно
-- разойдутся — ровно это случилось 29 августа с путём к файлу, который
-- считался в двух местах и разошёлся на первом же необычном письме.
--
-- Функция вызывается ВНУТРИ INSERT документа, одним оператором:
--     insert into doc.documents (..., mail_id)
--     values ($1, ..., doc.record_mail($8, $9, ...));
-- Поэтому письмо и документ записываются вместе или никак, и порядок
-- выполнения узлов на результат не влияет — его просто нет.
--
-- Без security definer: функция работает с правами вызывающего.
-- С security definer она выполнялась бы с правами владельца и стала бы
-- дырой в изоляции эталона.
-- ---------------------------------------------------------------------

create or replace function doc.record_mail(
    p_message_id       text,
    p_sender           text,
    p_subject          text,
    p_received_at      timestamptz,
    p_sender_approved  boolean,
    p_attachment_count int,
    p_pdf_count        int,
    p_status           text,
    p_skipped_files    text
) returns bigint
language plpgsql
as $$
declare
    v_id bigint;
begin
    -- Письмо без Message-ID опознать нечем: сравнение с пустотой
    -- в SQL никогда не даёт истины, и on conflict не сработает.
    -- Пишем новую строку. Лишняя запись дешевле потерянного следа.
    if p_message_id is null or p_message_id = '' then
        insert into doc.mails (
            message_id, sender, subject, received_at, sender_approved,
            attachment_count, pdf_count, status, skipped_files)
        values (
            null, p_sender, p_subject, p_received_at, p_sender_approved,
            p_attachment_count, p_pdf_count, p_status, p_skipped_files)
        returning id into v_id;
        return v_id;
    end if;

    -- Повторный прогон обновляет поля: в базе должен оказаться
    -- результат текущей логики, а не прошлой. created_at не трогаем —
    -- он говорит, когда письмо увидели впервые.
    insert into doc.mails (
        message_id, sender, subject, received_at, sender_approved,
        attachment_count, pdf_count, status, skipped_files)
    values (
        p_message_id, p_sender, p_subject, p_received_at, p_sender_approved,
        p_attachment_count, p_pdf_count, p_status, p_skipped_files)
    on conflict (message_id) do update set
        sender           = excluded.sender,
        subject          = excluded.subject,
        received_at      = excluded.received_at,
        sender_approved  = excluded.sender_approved,
        attachment_count = excluded.attachment_count,
        pdf_count        = excluded.pdf_count,
        status           = excluded.status,
        skipped_files    = excluded.skipped_files
    returning id into v_id;

    return v_id;
end $$;

comment on function doc.record_mail is
    'Записывает входящее письмо и возвращает его id. Единственное место вставки в doc.mails: вызывается и с ветки с PDF, и с ветки без.';

-- ---------------------------------------------------------------------
-- Права.
--
-- На саму таблицу права достанутся сами: в 07-prava.sql для схемы doc
-- заданы умолчания ALTER DEFAULT PRIVILEGES — проверено запросом к
-- pg_default_acl, там {bench_pipeline=arw/postgres, bench_score=r/postgres}.
--
-- А вот последовательности и функции умолчаниями НЕ покрыты: строк с
-- типом S и f для схемы doc в pg_default_acl нет. Счётчик id —
-- bigserial, то есть отдельная последовательность. Без usage на неё
-- insert под bench_pipeline упадёт с permission denied for sequence.
-- Без execute на функцию узлы WF-01 её не вызовут.
--
-- Формулировка для последовательности та же, что уже применена к
-- documents_id_seq и трём другим: bench_pipeline=rU/postgres.
-- ---------------------------------------------------------------------

grant usage, select on sequence doc.mails_id_seq to bench_pipeline;

grant execute on function doc.record_mail(
    text, text, text, timestamptz, boolean, int, int, text, text
) to bench_pipeline;

-- На случай, если умолчания не сработали (таблица создана не под
-- postgres): выдать явно. Повторная выдача уже имеющихся прав
-- ошибки не даёт.
grant select, insert, update on doc.mails to bench_pipeline;
grant select                 on doc.mails to bench_score;
