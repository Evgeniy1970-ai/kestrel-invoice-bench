-- =====================================================================
-- Kestrel Invoice Bench — схема rec, часть первая: прогоны
-- Отдельным файлом, потому что на rec.runs ссылаются таблицы схемы doc.
-- =====================================================================

create schema if not exists rec;
comment on schema rec is 'Сверка: прогоны, проверки строк и документов, исключения, журнал решений';

create table rec.runs (
    id                bigserial primary key,
    kind              text not null check (kind in ('debug','measured')),
    batch_code        text not null,
    model             text,
    prompt_version    text,
    settings_snapshot jsonb not null,
    started_at        timestamptz not null default now(),
    finished_at       timestamptz,
    docs_total        int,
    tokens_in         bigint,
    tokens_out        bigint,
    cost_usd          numeric(12,4),
    note              text
);

comment on table rec.runs is
    'Ось всей схемы результатов. Один прогон = один набор цифр, который можно '
    'сравнить с другим. Перезапуск с новым промптом создаёт новую строку, а не '
    'затирает старую.';
comment on column rec.runs.kind is
    'debug — отладочные 20 документов, measured — мерная партия 260.';
comment on column rec.runs.settings_snapshot is
    'Копия ref.settings на момент старта. Без неё сравнение двух прогонов '
    'превращается в спор о том, что именно менялось.';

-- Снять настройки в прогон одной строкой.
create or replace function rec.settings_snapshot()
returns jsonb
language sql
stable
as $$
    select jsonb_object_agg(key, value) from ref.settings;
$$;
