-- =====================================================================
-- Kestrel Invoice Bench — роли и права
--
-- Это тот самый пункт, ради которого разводились схемы. Если конвейер
-- ходит в базу под ролью, которая видит truth, все предыдущие файлы
-- превращаются в декорацию.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Пароли заменить перед выполнением. В n8n и в скриптах — только эти
-- две роли, никогда не postgres и не service_role.
-- ---------------------------------------------------------------------
create role bench_pipeline login password 'ЗАМЕНИТЬ';
create role bench_score    login password 'ЗАМЕНИТЬ';

-- bench_pipeline — приём, извлечение, сверка. Эталона не видит.
-- bench_score    — подсчёт метрик. Видит и результаты, и эталон, только на чтение.
-- COMMENT ON ROLE здесь намеренно не используется: в Supabase роль postgres
-- не суперпользователь, и команда может не пройти.

-- ---------------------------------------------------------------------
-- Конвейер: ref читает, doc и rec пишет
-- ---------------------------------------------------------------------
grant usage on schema ref, doc, rec to bench_pipeline;

grant select on all tables in schema ref to bench_pipeline;
grant insert on ref.sku_map to bench_pipeline;   -- решения проверяющего
grant usage, select on sequence ref.sku_map_id_seq to bench_pipeline;

grant select, insert, update on all tables in schema doc to bench_pipeline;
grant select, insert, update on all tables in schema rec to bench_pipeline;
grant usage, select on all sequences in schema doc to bench_pipeline;
grant usage, select on all sequences in schema rec to bench_pipeline;

-- Журнал решений только дописывается. Триггер это уже запрещает,
-- но право лучше не выдавать вовсе.
revoke update, delete on rec.decisions_log from bench_pipeline;
revoke delete on all tables in schema doc from bench_pipeline;

-- ---------------------------------------------------------------------
-- Эталон: конвейеру не виден
-- ---------------------------------------------------------------------
-- В PostgreSQL права на новую схему никому по умолчанию не выдаются,
-- так что доступа у bench_pipeline нет и без этой строки. Она стоит здесь,
-- чтобы намерение было записано явно и переживло любую будущую правку.
revoke all on schema truth from public, bench_pipeline;
revoke all on all tables in schema truth from public, bench_pipeline;

-- ---------------------------------------------------------------------
-- Подсчёт: читает всё, пишет только в score
-- ---------------------------------------------------------------------
grant usage on schema truth, doc, rec, ref, score to bench_score;
grant select on all tables in schema truth to bench_score;
grant select on all tables in schema doc   to bench_score;
grant select on all tables in schema rec   to bench_score;
grant select on all tables in schema ref   to bench_score;
grant select, insert, update, delete on all tables in schema score to bench_score;
grant usage, select on all sequences in schema score to bench_score;

-- ---------------------------------------------------------------------
-- То же для таблиц, которые появятся позже
-- ---------------------------------------------------------------------
alter default privileges in schema ref  grant select on tables to bench_pipeline, bench_score;
alter default privileges in schema doc  grant select, insert, update on tables to bench_pipeline;
alter default privileges in schema rec  grant select, insert, update on tables to bench_pipeline;
alter default privileges in schema doc  grant select on tables to bench_score;
alter default privileges in schema rec  grant select on tables to bench_score;
alter default privileges in schema truth grant select on tables to bench_score;
alter default privileges in schema score grant select, insert, update, delete on tables to bench_score;

-- ---------------------------------------------------------------------
-- Проверка изоляции. Выполнить под bench_pipeline — обе строки
-- обязаны завершиться ошибкой доступа.
-- ---------------------------------------------------------------------
-- set role bench_pipeline;
-- select count(*) from truth.defects;    -- ожидается: permission denied for schema truth
-- select count(*) from truth.documents;  -- ожидается: permission denied for schema truth
-- reset role;

-- ---------------------------------------------------------------------
-- Про Supabase отдельно
-- ---------------------------------------------------------------------
-- Ключ service_role обходит RLS и ходит под ролью, которая видит всё.
-- Если n8n подключается по нему, изоляция эталона не работает — вообще.
-- Подключение конвейера — только строкой Postgres под bench_pipeline.
-- Схемы ref, doc, rec, truth, score в Exposed schemas API не добавлять.
