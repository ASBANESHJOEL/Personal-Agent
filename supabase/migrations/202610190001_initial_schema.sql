-- Personal Agent Milestone 1 schema. Apply with Supabase CLI after linking a project.
create extension if not exists pgcrypto;

create table public.agent_state (
  user_id uuid primary key references auth.users(id) on delete cascade,
  globally_paused boolean not null default true,
  updated_at timestamptz not null default now()
);
create table public.platform_accounts (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  platform text not null, external_account_id text, display_name text,
  status text not null default 'disconnected' check (status in ('disconnected','connected','error')),
  credential_ref text, created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  unique (user_id, platform, external_account_id)
);
create table public.content_items (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  platform text, kind text not null check (kind in ('post','comment','draft','discovery')), body text,
  source_url text, status text not null default 'proposed', metadata jsonb not null default '{}',
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table public.agent_runs (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued', started_at timestamptz, finished_at timestamptz, summary jsonb not null default '{}', created_at timestamptz not null default now()
);
create table public.action_policies (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  action_type text not null, enabled boolean not null default false, requires_approval boolean not null default true,
  constraints jsonb not null default '{}', updated_at timestamptz not null default now(), unique (user_id, action_type)
);
create table public.action_queue (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  content_item_id uuid references public.content_items(id) on delete set null,
  action_type text not null, payload jsonb not null default '{}', status text not null default 'proposed'
    check (status in ('proposed','pending_approval','approved','rejected','blocked','executed','failed')),
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table public.approvals (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  action_id uuid not null references public.action_queue(id) on delete cascade,
  decision text not null check (decision in ('approved','rejected')), note text, decided_at timestamptz not null default now(),
  unique (user_id, action_id)
);
create table public.execution_attempts (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  action_id uuid not null references public.action_queue(id) on delete cascade,
  status text not null check (status in ('started','succeeded','failed','blocked')),
  provider_request_id text, result jsonb not null default '{}', attempted_at timestamptz not null default now()
);
create table public.feedback (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  content_item_id uuid references public.content_items(id) on delete cascade,
  rating smallint check (rating between -1 and 1), note text, created_at timestamptz not null default now()
);
create table public.interest_preferences (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  topic text not null, preference smallint not null default 1 check (preference between -1 and 1), created_at timestamptz not null default now(),
  unique (user_id, topic)
);
create table public.audit_logs (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  actor_id uuid references auth.users(id) on delete set null, event_type text not null,
  subject_type text, subject_id uuid, details jsonb not null default '{}', created_at timestamptz not null default now()
);

-- Every browser role is scoped to its authenticated user's rows. Writes to sensitive action state
-- should be routed through a server endpoint with service-role credentials held server-side.
do $$ declare table_name text; begin
  foreach table_name in array array['agent_state','platform_accounts','content_items','agent_runs','action_policies','action_queue','approvals','execution_attempts','feedback','interest_preferences','audit_logs'] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('create policy %I on public.%I for select to authenticated using (auth.uid() = user_id)', table_name || '_select_own', table_name);
  end loop;
end $$;

create policy interest_preferences_write_own on public.interest_preferences for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);
-- Service role bypasses RLS and must only be used by trusted backend code, never the browser.

create function public.initialize_personal_agent_state() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  insert into public.agent_state (user_id, globally_paused) values (new.id, true) on conflict (user_id) do nothing;
  return new;
end;
$$;
create trigger on_auth_user_created_personal_agent
  after insert on auth.users for each row execute procedure public.initialize_personal_agent_state();
