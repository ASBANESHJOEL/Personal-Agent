-- Move Global Pause to authenticated, user-scoped PostgREST storage.
-- Existing rows get legacy_import_complete=false so the first verified API request
-- can import that user's prior SQLite value exactly once before using cloud state.
alter table public.agent_state
  add column if not exists legacy_import_complete boolean not null default false;

grant usage on schema public to authenticated;
grant select, insert, update on table public.agent_state to authenticated;
revoke all on table public.agent_state from anon, public;

drop policy if exists agent_state_select_own on public.agent_state;
drop policy if exists agent_state_insert_own on public.agent_state;
drop policy if exists agent_state_update_own on public.agent_state;

create policy agent_state_select_own
  on public.agent_state for select to authenticated
  using ((select auth.uid()) = user_id);

create policy agent_state_insert_own
  on public.agent_state for insert to authenticated
  with check ((select auth.uid()) = user_id);

create policy agent_state_update_own
  on public.agent_state for update to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create or replace function public.set_agent_state_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists agent_state_updated_at on public.agent_state;
create trigger agent_state_updated_at
  before update on public.agent_state
  for each row execute function public.set_agent_state_updated_at();

notify pgrst, 'reload schema';
