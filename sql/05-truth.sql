-- =====================================================================
-- Kestrel Invoice Bench — схема truth: эталон
--
-- Заливается один раз из kestrel-april-2026-batch.xlsx и больше не меняется.
-- Конвейер доступа к этой схеме не имеет — см. 07-prava.sql.
-- =====================================================================

create schema if not exists truth;
comment on schema truth is
    'Эталон партии. Читает только схема score. Если сюда дотянется конвейер, '
    'все цифры прогона станут недействительными, и понять это по ним будет нельзя.';

-- ---------------------------------------------------------------------
-- Документы партии: 248 счетов + 12 дублей
-- ---------------------------------------------------------------------
create table truth.documents (
    doc_id          text primary key,
    invoice_number  text not null,
    supplier        text not null,
    venue           text not null,
    layout          char(1) not null check (layout in ('A','B','C')),
    received_date   date not null,
    line_count      int not null,
    total_eur       numeric(12,2) not null,
    file_kind       text not null,
    readability     numeric(4,3),
    softened        boolean not null default false,
    defects         text,
    duplicate_of    text references truth.documents,
    duplicate_kind  text check (duplicate_kind in ('same_file','rebuilt_file','new_number'))
);

comment on column truth.documents.file_kind is
    'Из листа Documents: «PDF с текстом», «скан: аккуратный скан», '
    '«скан: потрёпанный», «скан: снято телефоном». Главный разрез отчёта.';
comment on column truth.documents.readability is
    'Оценка читаемости скана. Для чистых PDF пусто.';
comment on column truth.documents.softened is
    'Документ пересобирался с ослабленной деградацией, чтобы остаться читаемым.';
comment on column truth.documents.total_eur is
    'Итог, напечатанный в документе. У дефектных счетов он и есть неправильный — '
    'сравнивать с ним извлечение можно, обвинять систему в расхождении нельзя.';

-- ---------------------------------------------------------------------
-- Журнал дефектов: 98 строк
-- ---------------------------------------------------------------------
create table truth.defects (
    id              bigserial primary key,
    doc_id          text not null references truth.documents,
    invoice_number  text not null,
    defect_type     text not null check (defect_type in (
                        'PRICE_MISMATCH','STALE_PRICE','UNIT_MISMATCH','UNKNOWN_ITEM',
                        'MISSING_DISCOUNT','WRONG_VAT_RATE','DUPLICATE_INVOICE','ARITHMETIC_ERROR')),
    supplier        text not null,
    venue           text not null,
    layout          char(1) not null,
    scan_kind       text,
    invoice_date    date,
    line_no         int,
    field           text not null,
    correct_value   text not null,
    invoiced_value  text not null,
    impact_eur      numeric(12,2),
    note            text
);

comment on column truth.defects.line_no is
    'Пусто у дефектов уровня документа: total, subtotal, document.';
comment on column truth.defects.field is
    'unit_price, description, document, discount_pct, unit, vat_rate, total, line_net, subtotal.';
comment on column truth.defects.impact_eur is
    'Отрицательное значение — недоплата в пользу заказчика. Тоже находка.';

create index defects_by_type on truth.defects (defect_type);
create index defects_by_doc on truth.defects (doc_id);

-- ---------------------------------------------------------------------
-- Сводка партии
-- ---------------------------------------------------------------------
create table truth.batch (
    key    text primary key,
    value  text not null
);

-- ---------------------------------------------------------------------
-- Эталон не меняется после заливки
-- ---------------------------------------------------------------------
create or replace function truth.deny_change()
returns trigger
language plpgsql
as $$
begin
    raise exception 'Эталон неизменен. Правка % запрещена: изменив его после прогона, вы измените и результат', tg_op;
end;
$$;

create trigger truth_documents_frozen
    before update or delete on truth.documents
    for each row execute function truth.deny_change();

create trigger truth_defects_frozen
    before update or delete on truth.defects
    for each row execute function truth.deny_change();

-- Заливка идёт вставкой, триггеры ей не мешают. Если понадобится перезалить —
-- alter table ... disable trigger, и это осознанное действие, а не опечатка.
