-- Harden initial action-policy values and remove unneeded state deletion.
-- Policy rows are initialized with conservative defaults by the authenticated API.
-- Prevent a user from pre-seeding a policy with weaker approval/enabled flags.
drop policy if exists action_policies_insert_own on public.action_policies;

create policy action_policies_insert_own
  on public.action_policies for insert to authenticated
  with check (
    (select auth.uid()) = user_id
    and (
      (action_type = 'save_discovery' and enabled = true and requires_approval = false)
      or (action_type = 'draft_post' and enabled = true and requires_approval = true)
      or (
        action_type in ('publish_post', 'public_comment', 'github_star')
        and enabled = false
        and requires_approval = true
      )
    )
  );

-- RLS already restricts state rows by user_id. Remove table-level DELETE as
-- defense in depth because the API has no state-deletion feature.
revoke delete on table public.agent_state from authenticated;

notify pgrst, 'reload schema';
