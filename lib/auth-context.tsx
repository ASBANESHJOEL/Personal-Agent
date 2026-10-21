"use client";
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { getSupabaseClient } from "./supabase";

type AuthStatus = "loading" | "setup-needed" | "signed-out" | "signed-in";
type AuthContextValue = {
  status: AuthStatus;
  session: Session | null;
  email: string | null;
  error: string | null;
  busy: boolean;
  signIn(email: string, password: string): Promise<void>;
  signOut(): Promise<void>;
};
const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [client, setClient] = useState<SupabaseClient | null>(null);

  useEffect(() => {
    const supabase = getSupabaseClient();
    setClient(supabase);
    if (!supabase) { setStatus("setup-needed"); return; }
    let active = true;
    const { data: listener } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) return;
      setSession(nextSession);
      setStatus(nextSession ? "signed-in" : "signed-out");
    });
    void supabase.auth.getSession().then(({ data, error: sessionError }) => {
      if (!active) return;
      if (sessionError) { setError(sessionError.message); setStatus("signed-out"); return; }
      setSession(data.session);
      setStatus(data.session ? "signed-in" : "signed-out");
    });
    return () => { active = false; listener.subscription.unsubscribe(); };
  }, []);

  async function signIn(email: string, password: string) {
    if (!client) { setError("Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY to configure sign-in."); return; }
    setBusy(true); setError(null);
    try {
      const { error: signInError } = await client.auth.signInWithPassword({ email, password });
      if (signInError) setError(signInError.message);
    } catch { setError("Supabase sign-in could not reach the authentication service."); }
    finally { setBusy(false); }
  }

  async function signOut() {
    if (!client) return;
    setBusy(true); setError(null);
    try {
      const { error: signOutError } = await client.auth.signOut();
      if (signOutError) setError(signOutError.message);
    } catch { setError("Sign-out could not reach the authentication service."); }
    finally { setBusy(false); }
  }

  const value = useMemo<AuthContextValue>(() => ({ status, session, email: session?.user.email ?? null, error, busy, signIn, signOut }), [status, session, error, busy, client]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used within AuthProvider");
  return value;
}
