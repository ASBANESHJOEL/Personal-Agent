-- Keep the Auth trigger as the only caller of this SECURITY DEFINER function.
-- Trigger execution does not require these application roles to have direct
-- EXECUTE privilege; the migration owner retains ownership privileges.
revoke execute on function public.initialize_personal_agent_state()
  from public, anon, authenticated;
