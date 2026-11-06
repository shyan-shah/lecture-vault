-- Lecture Vault + module chats: run once in the Supabase SQL editor.
-- Safe to re-run. Extends academic_notes, adds academic_resources and the academic_search function
-- that the Vault MCP academic_* tools use.

-- ------------------------------------------------------------ academic_notes (one row per lecture)
create table if not exists public.academic_notes (
  id uuid primary key default gen_random_uuid(),
  created_at timestamptz not null default now()
);

alter table public.academic_notes
  add column if not exists user_id uuid,
  add column if not exists source text,
  add column if not exists panopto_delivery_id text,
  add column if not exists panopto_url text,
  add column if not exists module_code text,
  add column if not exists title text,
  add column if not exists topic text,
  add column if not exists lecture_date date,
  add column if not exists duration_minutes int,
  add column if not exists difficulty text,            -- foundational / applied / stretch
  add column if not exists summary text,
  add column if not exists notes_markdown text,
  add column if not exists key_concepts jsonb default '[]'::jsonb,
  add column if not exists equations jsonb default '[]'::jsonb,
  add column if not exists definitions jsonb default '[]'::jsonb,
  add column if not exists worked_examples jsonb default '[]'::jsonb,
  add column if not exists exam_signals jsonb default '[]'::jsonb,
  add column if not exists admin jsonb default '[]'::jsonb,
  add column if not exists review_questions jsonb default '[]'::jsonb,
  add column if not exists gaps jsonb default '[]'::jsonb,
  add column if not exists material_files jsonb,
  add column if not exists material_comparison jsonb,
  add column if not exists exam_links jsonb default '[]'::jsonb,
  add column if not exists transcript text,
  add column if not exists transcript_source text,
  add column if not exists updated_at timestamptz default now();

alter table public.academic_notes
  add column if not exists fts tsvector generated always as (
    setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(topic, '') || ' ' || coalesce(summary, '')), 'B') ||
    setweight(to_tsvector('english', coalesce(notes_markdown, '')), 'C') ||
    setweight(to_tsvector('english', left(coalesce(transcript, ''), 400000)), 'D')
  ) stored;

create unique index if not exists academic_notes_panopto_delivery_id_key on public.academic_notes (panopto_delivery_id);
create index if not exists academic_notes_module_date_idx on public.academic_notes (user_id, module_code, lecture_date desc);
drop index if exists academic_notes_fts_idx;
create index if not exists academic_notes_fts_gin on public.academic_notes using gin (fts);

-- ------------------------------------------------------------ academic_resources (slides, packs, questions)
create table if not exists public.academic_resources (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  module_code text not null,
  kind text not null,                 -- slides | pack | questions
  file text not null,                 -- path relative to the materials folder
  file_hash text,
  chunk_index int not null,
  unit text,                          -- p | slide | part
  page_start int,
  page_end int,
  content text not null,
  updated_at timestamptz not null default now(),
  fts tsvector generated always as (
    setweight(to_tsvector('english', coalesce(file, '')), 'A') ||
    setweight(to_tsvector('english', coalesce(content, '')), 'C')
  ) stored,
  unique (user_id, file, chunk_index)
);
create index if not exists academic_resources_fts_gin on public.academic_resources using gin (fts);
create index if not exists academic_resources_module_idx on public.academic_resources (user_id, module_code, kind);
alter table public.academic_resources enable row level security;   -- service_role bypasses, like the other tables

-- ------------------------------------------------------------ ranked search across notes, transcripts and files
create or replace function public.academic_search(
  p_user uuid, p_query text, p_module text default null, p_kinds text[] default null,
  p_limit int default 12, p_any boolean default false
)
returns table (
  source text, kind text, module_code text, ref_id text, title text, lecture_date date,
  file text, page_start int, page_end int, unit text, rank real, snippet text
)
language sql stable as $$
  with q as (
    select case when p_any
      then to_tsquery('english', array_to_string(array(
             select quote_literal(w) || ':*' from regexp_split_to_table(lower(p_query), '[^a-z0-9]+') w
             where length(w) > 2), ' | '))
      else websearch_to_tsquery('english', p_query) end as tsq
  ),
  hits as (
    select 'lecture'::text as source,
           case when to_tsvector('english', coalesce(n.notes_markdown, '')) @@ q.tsq then 'notes' else 'transcript' end as kind,
           n.module_code, coalesce(n.panopto_delivery_id, n.id::text) as ref_id, n.title, n.lecture_date,
           null::text as file, null::int as page_start, null::int as page_end, null::text as unit,
           ts_rank_cd(n.fts, q.tsq)::real as rank,
           case when to_tsvector('english', coalesce(n.notes_markdown, '')) @@ q.tsq
             then ts_headline('english', n.notes_markdown, q.tsq, 'MaxWords=55,MinWords=20,MaxFragments=2')
             else ts_headline('english', left(coalesce(n.transcript, ''), 200000), q.tsq, 'MaxWords=55,MinWords=20,MaxFragments=2')
           end as snippet
    from public.academic_notes n, q
    where n.user_id = p_user and n.fts @@ q.tsq
      and (p_module is null or n.module_code = p_module)
      and (p_kinds is null or 'notes' = any(p_kinds) or 'transcript' = any(p_kinds))
    union all
    select 'resource', r.kind, r.module_code, r.id::text, r.file, null, r.file, r.page_start, r.page_end, r.unit,
           ts_rank_cd(r.fts, q.tsq)::real,
           ts_headline('english', r.content, q.tsq, 'MaxWords=55,MinWords=20,MaxFragments=2')
    from public.academic_resources r, q
    where r.user_id = p_user and r.fts @@ q.tsq
      and (p_module is null or r.module_code = p_module)
      and (p_kinds is null or r.kind = any(p_kinds))
  )
  select * from hits order by rank desc limit greatest(1, least(p_limit, 50));
$$;

-- Check for NOT NULL columns on academic_notes the app does not fill (if this returns rows, give them defaults):
select column_name, data_type
from information_schema.columns
where table_schema = 'public' and table_name = 'academic_notes'
  and is_nullable = 'NO' and column_default is null and column_name not in ('id');
