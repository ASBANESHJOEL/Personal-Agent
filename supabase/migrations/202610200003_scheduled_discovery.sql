-- Per-user opt-in for the daily discovery batch. No existing user is enabled.
create table public.discovery_schedules (
  user_id uuid primary key references auth.users(id) on delete cascade,
  enabled boolean not null default false
);
alter table public.discovery_schedules enable row level security;

create policy discovery_schedules_select_own
  on public.discovery_schedules for select to authenticated
  using ((select auth.uid()) = user_id);
create policy discovery_schedules_insert_own
  on public.discovery_schedules for insert to authenticated
  with check ((select auth.uid()) = user_id);
create policy discovery_schedules_update_own
  on public.discovery_schedules for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

revoke all privileges on table public.discovery_schedules from public, anon, authenticated;
grant select on table public.discovery_schedules to authenticated;
grant insert (user_id, enabled) on table public.discovery_schedules to authenticated;
grant update (enabled) on table public.discovery_schedules to authenticated;
grant select on table public.discovery_schedules to service_role;
grant usage on schema public to authenticated, service_role;

-- Keep existing agent_runs owner SELECT policy. Browser users can read history,
-- while only the isolated worker can create or update scheduled run rows.
alter table public.agent_runs
  add column if not exists run_type text not null default 'manual',
  add column if not exists source_results jsonb not null default '[]'::jsonb,
  add column if not exists new_discoveries integer not null default 0,
  add column if not exists updated_discoveries integer not null default 0,
  add column if not exists error_summary text,
  add column if not exists idempotency_key text,
  add column if not exists lease_expires_at timestamptz,
  add column if not exists attempt_count integer not null default 1;

create index if not exists agent_runs_user_recent_idx
  on public.agent_runs (user_id, started_at desc);
create unique index if not exists agent_runs_user_idempotency_key_unique
  on public.agent_runs (user_id, idempotency_key)
  where idempotency_key is not null;
create unique index if not exists agent_runs_one_active_scheduled_per_user_unique
  on public.agent_runs (user_id)
  where run_type = 'scheduled' and status = 'started';

revoke all privileges on table public.agent_runs from public, anon, authenticated;
grant select on table public.agent_runs to authenticated;
grant select on table public.agent_runs to service_role;
grant insert (user_id, status, started_at, run_type, source_results, new_discoveries, updated_discoveries, idempotency_key, lease_expires_at, attempt_count, summary)
  on table public.agent_runs to service_role;
grant update (status, started_at, finished_at, source_results, new_discoveries, updated_discoveries, error_summary, lease_expires_at, attempt_count, summary)
  on table public.agent_runs to service_role;
grant select on table public.agent_state to service_role;
grant select on table public.content_items to service_role;
grant insert (user_id, platform, kind, source_identity, source_url, body, status, metadata, ranking_score, ranking_reasons)
  on table public.content_items to service_role;

-- Claim one run per user atomically across dates. An existing live lease blocks
-- another run; expired work is marked failed and can be reclaimed safely.
create function public.claim_scheduled_discovery_run(
  p_user_id uuid,
  p_idempotency_key text,
  p_lease_seconds integer
) returns setof public.agent_runs
language plpgsql
security invoker
set search_path = ''
as $$
declare
  claimed public.agent_runs%rowtype;
  lease_until timestamptz := pg_catalog.now() + pg_catalog.make_interval(secs => least(greatest(coalesce(p_lease_seconds, 1800), 60), 7200));
begin
  -- Serialize claim decisions for a user while keeping different users concurrent.
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(p_user_id::text, 0));

  update public.agent_runs
    set status = 'failed',
        finished_at = pg_catalog.now(),
        lease_expires_at = null,
        error_summary = 'Previous scheduled run lease expired and was recovered.'
    where user_id = p_user_id
      and run_type = 'scheduled'
      and status = 'started'
      and (lease_expires_at is null or lease_expires_at <= pg_catalog.now());

  if exists (
    select 1 from public.agent_runs
    where user_id = p_user_id and run_type = 'scheduled'
      and status = 'started' and lease_expires_at > pg_catalog.now()
  ) then
    return;
  end if;

  update public.agent_runs
    set status = 'started',
        started_at = pg_catalog.now(),
        finished_at = null,
        source_results = '[]'::jsonb,
        new_discoveries = 0,
        updated_discoveries = 0,
        error_summary = null,
        lease_expires_at = lease_until,
        attempt_count = attempt_count + 1,
        summary = '{}'::jsonb
    where user_id = p_user_id
      and idempotency_key = p_idempotency_key
      and run_type = 'scheduled'
      and status in ('failed', 'partially_failed')
    returning * into claimed;
  if found then
    return next claimed;
    return;
  end if;

  if exists (
    select 1 from public.agent_runs
    where user_id = p_user_id and idempotency_key = p_idempotency_key
  ) then
    return;
  end if;

  insert into public.agent_runs (
    user_id, status, started_at, run_type, source_results,
    new_discoveries, updated_discoveries, idempotency_key,
    lease_expires_at, attempt_count, summary
  )
  values (
    p_user_id, 'started', pg_catalog.now(), 'scheduled', '[]'::jsonb,
    0, 0, p_idempotency_key, lease_until, 1, '{}'::jsonb
  )
  returning * into claimed;
  return next claimed;
  return;
end;
$$;
revoke all privileges on function public.claim_scheduled_discovery_run(uuid, text, integer) from public, anon, authenticated;
grant execute on function public.claim_scheduled_discovery_run(uuid, text, integer) to service_role;

notify pgrst, 'reload schema';
