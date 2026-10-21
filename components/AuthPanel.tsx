"use client";
import { useState, type FormEvent } from "react";
import { LogIn, LogOut, ShieldCheck, UserRound } from "lucide-react";
import { useAuth } from "../lib/auth-context";

export default function AuthPanel() {
  const auth = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await auth.signIn(email, password);
  }
  return <div className="rounded-xl border border-line bg-white p-3.5">
    <div className="flex items-center gap-2 text-xs font-semibold"><ShieldCheck size={15} className="text-green"/> Account security</div>
    {auth.status === "loading" && <p className="mb-0 mt-2 text-[11px] text-muted">Checking sign-in configuration…</p>}
    {auth.status === "setup-needed" && <p className="mb-0 mt-2 text-[11px] leading-relaxed text-muted">Sign-in setup needed. Configure <code>NEXT_PUBLIC_SUPABASE_URL</code> and <code>NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY</code> in the frontend environment, then restart Next.js.</p>}
    {auth.status === "signed-in" && <div className="mt-3 flex items-center gap-2"><span className="grid h-8 w-8 place-items-center rounded-full bg-[#edf5f0] text-green"><UserRound size={15}/></span><span className="min-w-0 flex-1 truncate text-[11px] font-medium">{auth.email}</span><button aria-label="Sign out" disabled={auth.busy} onClick={() => void auth.signOut()} className="rounded-md p-1.5 text-muted hover:bg-canvas disabled:opacity-50"><LogOut size={14}/></button></div>}
    {auth.status === "signed-out" && <form onSubmit={submit} className="mt-3 space-y-2"><label className="block text-[10px] font-medium text-muted">Email<input required type="email" autoComplete="email" value={email} onChange={(e)=>setEmail(e.target.value)} className="mt-1 h-8 w-full rounded-md border border-line px-2 text-xs text-ink outline-none focus:border-green"/></label><label className="block text-[10px] font-medium text-muted">Password<input required type="password" autoComplete="current-password" value={password} onChange={(e)=>setPassword(e.target.value)} className="mt-1 h-8 w-full rounded-md border border-line px-2 text-xs text-ink outline-none focus:border-green"/></label><button disabled={auth.busy} className="flex w-full items-center justify-center gap-2 rounded-lg bg-ink py-2 text-[11px] font-semibold text-white disabled:opacity-60"><LogIn size={13}/>{auth.busy?"Signing in…":"Sign in with Supabase"}</button></form>}
    {auth.error && <p role="alert" className="mb-0 mt-2 text-[10px] leading-relaxed text-red-700">{auth.error}</p>}
  </div>;
}
