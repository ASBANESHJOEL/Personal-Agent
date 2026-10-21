-- Persist public discovery metadata without changing existing content rows.
alter table public.content_items
  add column if not exists source_identity text,
  add column if not exists ranking_score numeric(5,1),
  add column if not exists ranking_reasons jsonb not null default '[]'::jsonb;

create unique index if not exists content_items_discovery_identity_unique
  on public.content_items (user_id, platform, source_identity);

grant usage on schema public to authenticated;
-- Remove inherited table-level access before adding the narrow grants below.
-- The owner and service_role retain their existing privileges.
revoke all privileges on table public.content_items from public, anon, authenticated;
grant select on table public.content_items to authenticated;
grant insert (user_id, platform, kind, source_identity, source_url, body, status, metadata, ranking_score, ranking_reasons)
  on table public.content_items to authenticated;
grant update (status) on table public.content_items to authenticated;

create policy content_items_insert_own
  on public.content_items for insert to authenticated
  with check ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'));

create policy content_items_update_own
  on public.content_items for update to authenticated
  using ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'))
  with check ((select auth.uid()) = user_id and kind = 'discovery' and status in ('proposed','saved','dismissed'));

notify pgrst, 'reload schema';
