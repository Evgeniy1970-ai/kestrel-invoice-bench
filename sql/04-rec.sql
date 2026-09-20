-- =====================================================================
-- Kestrel Invoice Bench — схема rec, часть вторая: сверка
-- Накатывать после 03-doc.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- Справочник флагов. Заодно связь с эталонными типами дефектов —
-- по ней схема score понимает, что чем поймано.
-- ---------------------------------------------------------------------
create table rec.flags (
    flag          text primary key,
    severity      text not null check (severity in ('none','medium','high')),
    level         text not null check (level in ('line','document')),
    truth_defect  text,
    is_service    boolean not null default false,
    note          text
);

insert into rec.flags (flag, severity, level, truth_defect, is_service, note) values
 ('MATCH',               'none',   'line',     null,                 false, 'В пределах допуска'),
 ('OVERCHARGE',          'high',   'line',     'PRICE_MISMATCH',     false, 'Цена выше договорной'),
 ('UNDERCHARGE',         'medium', 'line',     'PRICE_MISMATCH',     false, 'Цена ниже договорной'),
 ('STALE_PRICE',         'high',   'line',     'STALE_PRICE',        false, 'Цена соседнего периода, совпадает до цента'),
 ('UNIT_MISMATCH',       'high',   'line',     'UNIT_MISMATCH',      false, 'Единица не договорная'),
 ('MISSING_DISCOUNT',    'medium', 'line',     'MISSING_DISCOUNT',   false, 'Порог объёма превышен, скидки нет'),
 ('WRONG_VAT_RATE',      'high',   'line',     'WRONG_VAT_RATE',     false, 'Ставка не та, что положена позиции'),
 ('SKU_UNMATCHED',       'medium', 'line',     'UNKNOWN_ITEM',       false, 'Позиция не опознана'),
 ('NO_PRICE_FOUND',      'medium', 'line',     'UNKNOWN_ITEM',       false, 'Опознана, цены на дату нет'),
 ('ARITHMETIC_MISMATCH', 'high',   'document', 'ARITHMETIC_ERROR',   false, 'Документ противоречит сам себе'),
 ('DUPLICATE',           'high',   'document', 'DUPLICATE_INVOICE',  false, 'Документ уже был'),
 ('UNAPPROVED_SENDER',   'high',   'document', null,                 true,  'Письмо не от поставщика'),
 ('LOW_CONFIDENCE',      'medium', 'line',     null,                 true,  'Поле извлечено неуверенно'),
 ('NO_DELIVERY_DATE',    'medium', 'document', null,                 true,  'Дату поставки достать не удалось');

comment on column rec.flags.is_service is
    'Служебные флаги не считаются ложными тревогами: это отказ системы читать, '
    'а не ложное обвинение поставщика.';

-- ---------------------------------------------------------------------
-- Проверка строки
-- ---------------------------------------------------------------------
create table rec.line_checks (
    id                    bigserial primary key,
    run_id                bigint not null references rec.runs,
    invoice_line_id       bigint not null references doc.invoice_lines on delete cascade,

    matched_sku           text references ref.catalog_items,
    match_method          text check (match_method in ('vendor_sku','normalized','manual','none')),

    price_date            date,
    price_version_used    text references ref.price_versions,
    expected_unit_price   numeric(12,4),
    expected_discount_pct numeric(5,4),
    expected_vat_rate     numeric(5,4),
    unit_factor_applied   numeric(12,4),

    variance_abs          numeric(12,4),
    variance_pct          numeric(8,4),
    flag                  text not null references rec.flags,

    unique (run_id, invoice_line_id)
);

comment on column rec.line_checks.price_date is
    'Дата, по которой поднималась цена. Записывается всегда: при разборе '
    'пропущенного STALE_PRICE первый вопрос — какую дату взяли.';
comment on column rec.line_checks.unit_factor_applied is
    'Коэффициент из ref.catalog_items.alt_unit_factor, если цена приводилась '
    'к договорной единице.';

create index line_checks_by_flag on rec.line_checks (run_id, flag);

-- ---------------------------------------------------------------------
-- Проверка документа
-- ---------------------------------------------------------------------
create table rec.doc_checks (
    id             bigserial primary key,
    run_id         bigint not null references rec.runs,
    invoice_id     bigint not null references doc.invoices on delete cascade,

    status         text not null check (status in ('AUTO_APPROVED','NEEDS_REVIEW','BLOCKED')),
    arithmetic_ok  boolean,
    flags          text[] not null default '{}',

    duplicate_of   bigint references doc.invoices,
    duplicate_kind text check (duplicate_kind in ('same_file','rebuilt_file','new_number')),

    unique (run_id, invoice_id)
);

comment on column rec.doc_checks.duplicate_kind is
    'Три разновидности по возрастанию трудности: same_file — по отпечатку файла; '
    'rebuilt_file — по паре поставщик плюс номер; new_number — по тройке '
    'поставщик, дата, итог. Все три в партии есть.';

-- ---------------------------------------------------------------------
-- Исключения: то, что уходит человеку
-- ---------------------------------------------------------------------
create table rec.exceptions (
    id           bigserial primary key,
    run_id       bigint not null references rec.runs,
    invoice_id   bigint references doc.invoices on delete cascade,
    line_id      bigint references doc.invoice_lines on delete cascade,
    document_id  bigint references doc.documents,

    type         text not null references rec.flags,
    severity     text not null,
    message      text,
    impact_eur   numeric(12,2),

    resolved_by  text,
    resolved_at  timestamptz,
    resolution   text,
    note         text,
    created_at   timestamptz not null default now()
);

create index exceptions_open on rec.exceptions (run_id, type) where resolved_at is null;

-- ---------------------------------------------------------------------
-- Журнал решений. Только вставка.
-- ---------------------------------------------------------------------
create table rec.decisions_log (
    id           bigserial primary key,
    run_id       bigint references rec.runs,
    entity_type  text not null,
    entity_id    text not null,
    action       text not null,
    actor        text not null,
    before       jsonb,
    after        jsonb,
    created_at   timestamptz not null default now()
);

create or replace function rec.deny_change()
returns trigger
language plpgsql
as $$
begin
    raise exception 'rec.decisions_log только дописывается: % запрещён', tg_op;
end;
$$;

create trigger decisions_log_append_only
    before update or delete on rec.decisions_log
    for each row execute function rec.deny_change();

comment on table rec.decisions_log is
    'Ни UPDATE, ни DELETE. Триггер — вторая линия после отзыва прав: права можно '
    'выдать по невнимательности, триггер придётся снимать осознанно.';

-- ---------------------------------------------------------------------
-- Очередь обработки
-- ---------------------------------------------------------------------
create table rec.job_queue (
    id            bigserial primary key,
    document_id   bigint not null references doc.documents,
    run_id        bigint not null references rec.runs,
    stage         text not null check (stage in ('normalize','extract','reconcile')),
    status        text not null default 'pending'
                  check (status in ('pending','running','done','failed')),
    attempts      int not null default 0,
    last_error    text,
    scheduled_at  timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    unique (document_id, run_id, stage)
);

comment on table rec.job_queue is
    'Очередь нужна не ради нагрузки, а ради изоляции сбоев: упавший документ '
    'остаётся здесь со статусом failed и текстом ошибки, а не теряется молча '
    'внутри упавшего выполнения workflow.';

create index job_queue_pending on rec.job_queue (status, scheduled_at) where status = 'pending';
