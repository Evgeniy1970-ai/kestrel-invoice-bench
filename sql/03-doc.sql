-- =====================================================================
-- Kestrel Invoice Bench — схема doc: входящие документы и извлечение
-- =====================================================================

create schema if not exists doc;
comment on schema doc is 'Принятые файлы и то, что из них извлечено';

-- ---------------------------------------------------------------------
-- Документы. Живут вне прогонов: файл принят один раз.
-- ---------------------------------------------------------------------
create table doc.documents (
    id                 bigserial primary key,
    file_sha256        text not null unique,
    storage_path       text not null,
    source_filename    text,
    source_message_id  text,
    sender             text,
    received_at        timestamptz not null,
    page_count         int,
    has_text_layer     boolean,
    ingest_status      text not null default 'received'
                               check (ingest_status in ('received','duplicate_file','rejected','unapproved_sender')),
    created_at         timestamptz not null default now()
);

comment on column doc.documents.file_sha256 is
    'Отпечаток содержимого. Ловит первую разновидность дубля — тот же файл — '
    'ещё до всякого извлечения.';
comment on column doc.documents.source_filename is
    'Имя файла из письма. По нему схема score сопоставляет документ с эталоном. '
    'Сам конвейер его не читает.';
comment on column doc.documents.has_text_layer is
    'Есть ли в PDF текстовый слой. Определяет путь нормализации и даёт разрез '
    '«чистые PDF против сканов» в отчёте.';

create index documents_by_received on doc.documents (received_at);

-- ---------------------------------------------------------------------
-- Извлечение: один вызов модели по одному документу в рамках прогона
-- ---------------------------------------------------------------------
create table doc.extractions (
    id              bigserial primary key,
    document_id     bigint not null references doc.documents,
    run_id          bigint not null references rec.runs,
    model           text not null,
    prompt_version  text not null,
    raw_json        jsonb not null,
    tokens_in       int,
    tokens_out      int,
    cost_usd        numeric(10,6),
    duration_ms     int,
    ocr_provider    text,
    created_at      timestamptz not null default now(),

    unique (document_id, run_id)
);

comment on column doc.extractions.raw_json is
    'Ответ модели как есть, до всякой обработки. Хранится целиком: при разборе '
    'пропуска надо видеть, что модель на самом деле вернула, а не то, что мы из '
    'этого разобрали.';
comment on column doc.extractions.ocr_provider is
    'NULL, пока внешний распознаватель не введён. Решение принимается на этапе 6 по цифрам.';

-- ---------------------------------------------------------------------
-- Счёт
-- ---------------------------------------------------------------------
create table doc.invoices (
    id              bigserial primary key,
    extraction_id   bigint not null references doc.extractions,
    document_id     bigint not null references doc.documents,
    run_id          bigint not null references rec.runs,

    supplier_code   text references ref.suppliers,
    venue_code      text references ref.venues,
    invoice_number  text,
    invoice_date    date,
    delivery_date   date,
    period_from     date,
    period_to       date,
    waybill_ref     text,

    subtotal        numeric(12,2),
    vat_amount      numeric(12,2),
    total           numeric(12,2),

    layout          char(1),
    status          text not null default 'extracted',

    unique (extraction_id)
);

comment on column doc.invoices.delivery_date is
    'Пусто допустимо: на макете C отдельного поля нет, дата вписана в ссылку на '
    'накладную и достаётся разбором текста. Если достать не удалось — исключение '
    'NO_DELIVERY_DATE, документ уходит человеку. Откат к invoice_date запрещён.';
comment on column doc.invoices.period_from is
    'Для счетов за период вместо даты поставки.';
comment on column doc.invoices.supplier_code is
    'Пусто допустимо: поставщик может быть не опознан. Это не повод падать.';

create index invoices_by_run on doc.invoices (run_id);
create index invoices_by_number on doc.invoices (supplier_code, invoice_number);
-- Вторая разновидность дубля: тот же счёт в пересобранном файле.
-- Индекс не уникальный намеренно — дубль надо зафиксировать, а не отвергнуть
-- на уровне базы, иначе он не попадёт в метрики.

-- ---------------------------------------------------------------------
-- Строки счёта
-- ---------------------------------------------------------------------
create table doc.invoice_lines (
    id               bigserial primary key,
    invoice_id       bigint not null references doc.invoices on delete cascade,
    run_id           bigint not null references rec.runs,
    line_no          int not null,

    raw_description  text,
    vendor_sku       text,
    quantity         numeric(12,3),
    unit             text,
    unit_price       numeric(12,4),
    discount_pct     numeric(5,4),
    vat_rate         numeric(5,4),
    line_net         numeric(12,2),

    source_text      text,
    page             int,
    confidence       numeric(4,3),

    unique (invoice_id, line_no)
);

comment on column doc.invoice_lines.unit_price is
    'Четыре знака после запятой обязательны. В прайсе есть 5.5680 и 0.8340: '
    'сезонная надбавка считалась процентом. Округление до копеек создаёт '
    'расхождения в пару центов там, где их нет.';
comment on column doc.invoice_lines.vat_rate is
    'Пусто допустимо: на макете C построчного НДС нет, ставка выводится из '
    'итоговой разбивки.';
comment on column doc.invoice_lines.discount_pct is
    'Пусто допустимо: на макете C скидка спрятана в описании позиции.';
comment on column doc.invoice_lines.source_text is
    'Фрагмент документа, из которого взято значение. Без него проверяющий видит '
    'мнение модели вместо доказательства, и систему не примет ни один бухгалтер.';

create index lines_by_run on doc.invoice_lines (run_id);
