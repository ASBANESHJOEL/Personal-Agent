-- Allow only owner-scoped API operations needed for policy reads/defaults and
-- explicit approvals. Queue creation/execution and audit writes remain trusted
-- server-side operations and are intentionally not granted to browser roles.

revoke all privileges on table public.action_policies, public.action_queue,
  public.approvals, public.execution_attempts, public.audit_logs
  from public, anon, authenticated;

grant select on table public.action_policies, public.action_queue,
  public.approvals, public.execution_attempts, public.audit_logs to authenticated;

-- Policy defaults are inserted by the authenticated API using the verified
-- user's JWT. Existing policy choices are never overwritten by an upsert.
grant insert (user_id, action_type, enabled, requires_approval, constraints)
  on table public.action_policies to authenticated;
create policy action_policies_insert_own
  on public.action_policies for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and action_type in ('save_discovery', 'draft_post', 'publish_post', 'public_comment', 'github_star')
  );

-- A user can record an explicit decision only for their own queued action.
-- The action row itself must be in the pending approval state.
grant insert (user_id, action_id, decision, note)
  on table public.approvals to authenticated;
create policy approvals_insert_own_action
  on public.approvals for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and exists (
      select 1
      from public.action_queue q
      where q.id = action_id
        and q.user_id = (select auth.uid())
        and q.status = 'pending_approval'
    )
  );

-- Preserve the owner-scoped SELECT policies created by migration 001. No
-- authenticated UPDATE/DELETE is granted on these tables. The app currently
-- has no browser-facing queue, attempt, or audit-log write operation.

notify pgrst, 'reload schema';
