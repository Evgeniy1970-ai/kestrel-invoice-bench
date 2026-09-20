-- =====================================================================
-- Kestrel Invoice Bench — проверки этапа 4
--
-- Выполнять после заливки справочников и эталона. Этап считается
-- закрытым, когда все семь дают ожидаемый результат.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. ГЛАВНАЯ. Цена на дату, а не сегодняшняя.
--    Romaine lettuce у Greenfield Produce: 4.20 в марте, 4.35 в первой
--    половине апреля, 5.5680 с 16 апреля.
-- ---------------------------------------------------------------------
select '28 марта' as дата, * from ref.price_on('VEG-0101', '2026-03-28')
union all
select '10 апреля', * from ref.price_on('VEG-0101', '2026-04-10')
union all
select '20 апреля', * from ref.price_on('VEG-0101', '2026-04-20');

-- Ожидается ровно три строки:
--   28 марта   4.2000  kg  PL-2026-Q1
--   10 апреля  4.3500  kg  PL-2026-Q2
--   20 апреля  5.5680  kg  PL-2026-04-VEG
-- Если на 28 марта вернулась апрельская цена — сверка на дату не работает,
-- и двенадцать STALE_PRICE не будут пойманы ни при каких промптах.

-- ---------------------------------------------------------------------
-- 2. Ни одна позиция не даёт двух цен на одну дату
-- ---------------------------------------------------------------------
select sku, count(*)
from ref.prices p, generate_series('2026-01-01'::date, '2026-06-30'::date, '1 day') d
where d.d between p.valid_from and p.valid_to
group by sku, d.d
having count(*) > 1;
-- Ожидается: пусто. При непустом результате виновата загрузка —
-- ограничение prices_no_overlap такое просто не пропустит.

-- ---------------------------------------------------------------------
-- 3. Справочники загружены целиком
-- ---------------------------------------------------------------------
select 'venues' as таблица, count(*) as строк, 4   as ожидается from ref.venues
union all select 'suppliers',      count(*), 12  from ref.suppliers
union all select 'catalog_items',  count(*), 104 from ref.catalog_items
union all select 'price_versions', count(*), 3   from ref.price_versions
union all select 'prices',         count(*), 216 from ref.prices;

-- ---------------------------------------------------------------------
-- 4. Эталон загружен целиком
-- ---------------------------------------------------------------------
select 'truth.documents' as таблица, count(*) as строк, 260 as ожидается from truth.documents
union all select 'truth.defects', count(*), 98 from truth.defects
union all select '  из них дублей',
       count(*) filter (where duplicate_of is not null), 12 from truth.documents
union all select '  документов с дефектом',
       count(distinct doc_id), 90 from truth.defects;

-- Разбивка по типам. Ожидается: PRICE_MISMATCH 18, UNKNOWN_ITEM 14,
-- ARITHMETIC_ERROR 12, DUPLICATE_INVOICE 12, STALE_PRICE 12,
-- MISSING_DISCOUNT 10, UNIT_MISMATCH 10, WRONG_VAT_RATE 10.
select defect_type, count(*) from truth.defects group by 1 order by 2 desc, 1;

-- ---------------------------------------------------------------------
-- 5. Сезонная версия касается только овощей
-- ---------------------------------------------------------------------
select v.version_code, v.scope_supplier, count(p.sku) as позиций
from ref.price_versions v
left join ref.prices p on p.version_code = v.version_code
group by 1, 2 order by 1;
-- Ожидается: PL-2026-04-VEG — scope VEG, 8 позиций.
-- Q1 и Q2 — scope пустой, по 104.

-- ---------------------------------------------------------------------
-- 6. Эталон неизменен
-- ---------------------------------------------------------------------
-- update truth.defects set impact_eur = 0 where id = 1;
-- Ожидается: ошибка «Эталон неизменен».

-- ---------------------------------------------------------------------
-- 7. Изоляция эталона
-- ---------------------------------------------------------------------
-- Выполнять под bench_pipeline, не под владельцем базы:
-- select count(*) from truth.defects;
-- Ожидается: permission denied for schema truth.
--
-- Если запрос отработал и вернул 98 — значит подключение идёт под ролью
-- с полным доступом, и всё разделение схем ничего не стоит.
