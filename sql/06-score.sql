-- =====================================================================
-- Kestrel Invoice Bench — схема score: сравнение прогона с эталоном
--
-- Единственное место, которое читает и rec, и truth. Запускается после
-- прогона отдельным шагом и ничего в конвейер не возвращает.
-- =====================================================================

create schema if not exists score;
comment on schema score is 'Метрики прогона: что поймано, что пропущено, где ложная тревога';

-- ---------------------------------------------------------------------
-- Сопоставление принятых документов с эталонными
-- ---------------------------------------------------------------------
create table score.doc_map (
    run_id        bigint not null references rec.runs,
    document_id   bigint not null references doc.documents,
    truth_doc_id  text not null references truth.documents,
    matched_by    text not null check (matched_by in ('filename','sha256','invoice_number','manual')),

    primary key (run_id, document_id)
);

comment on table score.doc_map is
    'Строится один раз в начале подсчёта по имени файла. Всё остальное в этой '
    'схеме опирается на него.';

-- ---------------------------------------------------------------------
-- Главная таблица: судьба каждого заложенного дефекта
-- ---------------------------------------------------------------------
create table score.defects (
    id                   bigserial primary key,
    run_id               bigint not null references rec.runs,
    truth_defect_id      bigint not null references truth.defects,
    status               text not null check (status in ('caught','missed','partial')),
    matched_exception_id bigint references rec.exceptions,
    caught_as            text,
    note                 text,

    unique (run_id, truth_defect_id)
);

comment on column score.defects.status is
    'partial — дефект замечен, но названо не то: например PRICE_MISMATCH вместо '
    'STALE_PRICE. Строка ушла человеку, значит переплата не пройдёт, но разговор '
    'с поставщиком будет не тот. В отчёте показывается отдельно от caught.';
comment on column score.defects.caught_as is
    'Флаг, которым система его пометила. Может не совпадать с ожидаемым.';

-- ---------------------------------------------------------------------
-- Ложные тревоги
-- ---------------------------------------------------------------------
create table score.false_alarms (
    id            bigserial primary key,
    run_id        bigint not null references rec.runs,
    exception_id  bigint not null references rec.exceptions,
    type          text not null,
    truth_doc_id  text references truth.documents,
    note          text,

    unique (run_id, exception_id)
);

comment on table score.false_alarms is
    'Исключения, которым не соответствует ни одна строка журнала. Служебные флаги '
    'сюда не попадают: NO_DELIVERY_DATE и LOW_CONFIDENCE — отказ читать, а не '
    'ложное обвинение. Считать их ложными тревогами значит наказывать систему за '
    'честность.';

-- ---------------------------------------------------------------------
-- Точность извлечения по полям, для которых эталон есть
-- ---------------------------------------------------------------------
create table score.fields (
    id               bigserial primary key,
    run_id           bigint not null references rec.runs,
    truth_doc_id     text not null references truth.documents,
    field            text not null,
    truth_value      text,
    extracted_value  text,
    is_match         boolean not null,

    unique (run_id, truth_doc_id, field)
);

comment on table score.fields is
    'Поля из листа Documents: invoice_number, supplier, venue, received_date, '
    'line_count, total_eur. Построчного эталона по всей партии не существует — '
    'генератор не сохранился. Ручная выборка на 25 документов отложена до конца этапа 6.';

-- ---------------------------------------------------------------------
-- Проверка на выдумывание
-- ---------------------------------------------------------------------
create table score.number_check (
    id             bigserial primary key,
    run_id         bigint not null references rec.runs,
    document_id    bigint not null references doc.documents,
    numbers_total  int not null,
    numbers_unseen int not null,
    unseen_sample  text,

    unique (run_id, document_id)
);

comment on table score.number_check is
    'Каждое число, вернувшееся от модели, должно встречаться среди чисел, '
    'напечатанных в текстовом слое документа. Работает только на 208 чистых PDF. '
    'Ловит выдуманные значения; пропуск строки не ловит — отсутствующие числа '
    'проверку не нарушают.';

-- ---------------------------------------------------------------------
-- Итоговые метрики
-- ---------------------------------------------------------------------
create table score.summary (
    run_id   bigint not null references rec.runs,
    metric   text not null,
    segment  text not null default 'all',
    value    numeric(14,4),
    note     text,

    primary key (run_id, metric, segment)
);

comment on column score.summary.segment is
    'Разрезы, без которых цифра врёт: all, layout_A/B/C, pdf_text, scan_clean, '
    'scan_worn, scan_phone. Для MISSING_DISCOUNT и WRONG_VAT_RATE общая цифра не '
    'публикуется вовсе: дефекты этих типов перекошены в сторону сканов (6 из 10 и '
    '5 из 10 при доле сканов 20%), и низкая точность будет выглядеть как проблема '
    'сверки, хотя причина — качество картинки.';
