-- =====================================================================
-- Kestrel Invoice Bench — схема ref: справочники
-- Накатывать первой. Требует прав владельца базы.
-- =====================================================================

-- Supabase держит расширения в отдельной схеме extensions. Ставим туда же
-- и подмешиваем её в search_path: классы операторов из btree_gist нужны
-- в момент создания ограничения непересечения ниже.
create schema if not exists extensions;
create extension if not exists btree_gist with schema extensions;
set search_path = public, extensions;

create schema if not exists ref;
comment on schema ref is 'Справочники: заведения, поставщики, каталог, цены, настройки';

-- ---------------------------------------------------------------------
-- Заведения
-- ---------------------------------------------------------------------
create table ref.venues (
    venue_code  text primary key,
    name        text not null unique,
    venue_type  text not null,
    address     text
);

-- ---------------------------------------------------------------------
-- Поставщики
-- ---------------------------------------------------------------------
create table ref.suppliers (
    supplier_code       text primary key,
    name                text not null unique,
    category            text,
    billing_email       text,
    payment_terms       text,
    layout              char(1)  check (layout in ('A','B','C')),
    billing_mode        text     check (billing_mode in ('per_delivery','per_period')),
    invoice_no_pattern  text,
    status              text not null default 'approved'
                        check (status in ('approved','blocked'))
);

comment on column ref.suppliers.layout is
    'Макет счёта. Системой не используется — только для разрезов в отчёте.';
comment on column ref.suppliers.status is
    'Письмо от поставщика со статусом blocked уходит в исключение UNAPPROVED_SENDER.';

-- ---------------------------------------------------------------------
-- Каталог позиций
-- ---------------------------------------------------------------------
create table ref.catalog_items (
    sku                     text primary key,
    supplier_code           text not null references ref.suppliers,
    description             text not null,
    unit                    text not null,
    alt_unit                text,
    alt_unit_factor         numeric(12,4),
    vat_rate                numeric(5,4) not null,
    discount_threshold_qty  numeric(12,3),
    discount_pct            numeric(5,4),

    constraint alt_unit_pair check ((alt_unit is null) = (alt_unit_factor is null)),
    constraint discount_pair check ((discount_threshold_qty is null) = (discount_pct is null)),
    constraint alt_factor_positive check (alt_unit_factor is null or alt_unit_factor > 0)
);

comment on column ref.catalog_items.vat_rate is
    'Доля, не проценты: 0.1000 или 0.2000. В том же виде, что в прайсе.';
comment on column ref.catalog_items.alt_unit_factor is
    'Сколько договорных единиц в альтернативной. Через него разбирается UNIT_MISMATCH.';

create index catalog_by_supplier on ref.catalog_items (supplier_code);
create index catalog_by_description on ref.catalog_items (lower(description));

-- ---------------------------------------------------------------------
-- Версии прайса
-- ---------------------------------------------------------------------
create table ref.price_versions (
    version_code    text primary key,
    effective_from  date not null,
    effective_to    date not null,
    scope_supplier  text references ref.suppliers,
    note            text,

    constraint version_dates check (effective_to >= effective_from)
);

comment on column ref.price_versions.scope_supplier is
    'NULL — версия для всех поставщиков. PL-2026-04-VEG касается только VEG.';

-- ---------------------------------------------------------------------
-- Цены с датами действия
-- ---------------------------------------------------------------------
create table ref.prices (
    sku           text not null references ref.catalog_items,
    version_code  text not null references ref.price_versions,
    unit          text not null,
    unit_price    numeric(12,4) not null check (unit_price > 0),
    valid_from    date not null,
    valid_to      date not null,

    primary key (sku, valid_from),
    constraint price_dates check (valid_to >= valid_from)
);

-- Периоды действия одной позиции не пересекаются. Это не пожелание,
-- а условие, при котором поиск цены на дату возвращает ровно одну строку.
-- В прайсе оно соблюдено: для восьми овощных позиций PL-2026-Q2 закрыта
-- 15 апреля, PL-2026-04-VEG открывается 16-го. Ограничение не даёт
-- сломать это будущей загрузкой.
alter table ref.prices add constraint prices_no_overlap
    exclude using gist (
        sku with =,
        daterange(valid_from, valid_to, '[]') with &&
    );

create index prices_lookup on ref.prices (sku, valid_from, valid_to);

-- ---------------------------------------------------------------------
-- Сопоставление позиций поставщика с каталогом
-- ---------------------------------------------------------------------
create table ref.sku_map (
    id                  bigserial primary key,
    supplier_code       text not null references ref.suppliers,
    vendor_sku          text,
    vendor_description  text,
    sku                 text not null references ref.catalog_items,
    match_method        text not null
                        check (match_method in ('vendor_sku','normalized','manual')),
    confidence          numeric(4,3),
    created_by          text not null default 'system',
    run_id              bigint,
    created_at          timestamptz not null default now(),

    constraint sku_map_has_key check (vendor_sku is not null or vendor_description is not null)
);

comment on table ref.sku_map is
    'Пополняется решениями проверяющего. То, что человек сопоставил руками, со второго раза опознаётся само.';

create unique index sku_map_by_code on ref.sku_map (supplier_code, vendor_sku)
    where vendor_sku is not null;
create unique index sku_map_by_text on ref.sku_map (supplier_code, lower(vendor_description))
    where vendor_description is not null;

-- ---------------------------------------------------------------------
-- Настройки: допуски и пороги
-- ---------------------------------------------------------------------
create table ref.settings (
    key    text primary key,
    value  jsonb not null,
    note   text
);

insert into ref.settings (key, value, note) values
    ('tolerance_abs_eur',        '0.01',  'Расхождение цены меньше этого — норма'),
    ('tolerance_pct',            '0.005', 'То же в долях: 0.5%'),
    ('arithmetic_tolerance_eur', '0.02',  'Допуск на округление при проверке арифметики строки'),
    ('low_confidence_threshold', '0.85',  'Черновое значение. Выставляется по первому прогону'),
    ('vat_rates',                '[0.10, 0.20]', 'Льготная и стандартная'),
    ('period_price_date_rule',   '"period_from"',
     'По какой дате искать цену для счёта за период. Предположение, проверяется на хвосте марта');

-- ---------------------------------------------------------------------
-- Цена на дату. Единственное место, где она ищется.
-- ---------------------------------------------------------------------
create or replace function ref.price_on(p_sku text, p_date date)
returns table (unit_price numeric, unit text, version_code text)
language sql
stable
as $$
    select p.unit_price, p.unit, p.version_code
    from ref.prices p
    where p.sku = p_sku
      and p_date between p.valid_from and p.valid_to;
$$;

comment on function ref.price_on is
    'Цена, действовавшая на дату ПОСТАВКИ. Передавать сюда дату счёта нельзя: '
    'на этом рассыпается весь STALE_PRICE.';

-- Соседняя версия — для отличия STALE_PRICE от PRICE_MISMATCH.
-- Если выставленная цена до цента совпадает с ценой из другого периода,
-- это не завышение, а не тот прайс.
create or replace function ref.price_other_periods(p_sku text, p_date date)
returns table (unit_price numeric, version_code text, valid_from date, valid_to date)
language sql
stable
as $$
    select p.unit_price, p.version_code, p.valid_from, p.valid_to
    from ref.prices p
    where p.sku = p_sku
      and p_date not between p.valid_from and p.valid_to;
$$;
