-- ============================================================================
-- CLC Control Inteligente de Stock — Esquema de base de datos (Supabase / Postgres)
-- ----------------------------------------------------------------------------
-- Cómo usarlo:
--   1. En Supabase, ir a "SQL Editor".
--   2. Pegar TODO este archivo.
--   3. Apretar "Run".
-- Esto crea las tablas y el bucket de archivos. Se puede correr más de una vez
-- sin romper nada (usa IF NOT EXISTS).
-- ============================================================================

-- Clientes: cada empresa para la que CLC controla stock.
create table if not exists clientes (
    id          bigint generated always as identity primary key,
    nombre      text not null unique,
    creado_en   timestamptz not null default now()
);

-- Corridas: cada análisis ejecutado para un cliente queda guardado con su historial.
create table if not exists corridas (
    id                    bigint generated always as identity primary key,
    cliente_id            bigint not null references clientes(id) on delete cascade,
    creado_en             timestamptz not null default now(),
    -- Parámetros usados en el análisis (use_deposit, include_initial, assume_zero, etc.)
    parametros            jsonb,
    -- KPIs del resumen (total controlados, ok, con diferencia, diferencia absoluta).
    resumen               jsonb,
    -- Rutas dentro del bucket de Storage donde quedan los archivos de esta corrida.
    archivo_control       text,   -- parquet con la tabla de control completa
    archivo_reporte_xlsx  text,   -- xlsx descargable
    archivo_diagnostico   text,   -- txt del diagnóstico
    nota                  text
);

create index if not exists idx_corridas_cliente on corridas (cliente_id, creado_en desc);

-- ----------------------------------------------------------------------------
-- Storage: bucket donde se guardan los archivos (parquet/xlsx/txt) de cada corrida.
-- ----------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('reportes', 'reportes', false)
on conflict (id) do nothing;
