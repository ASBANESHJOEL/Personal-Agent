"use client";
import { LoaderCircle, LockKeyhole, RefreshCw } from "lucide-react";
import { useAgentPause } from "../lib/use-agent-pause";

export default function GlobalPauseControl({ accessToken }: { accessToken: string | null }) {
  const pause = useAgentPause(accessToken);
  const isLoading = pause.status === "loading";
  const title = pause.status === "auth-required" ? "Sign in to load server pause state"
    : isLoading ? "Loading server pause state…"
    : pause.status === "error" ? "Pause state could not be confirmed"
    : pause.paused ? "Global pause is on" : "Global pause is off";
  const detail = pause.status === "auth-required" ? "Configure Supabase Auth and sign in. Pause controls stay disabled until the server confirms the current state."
    : pause.status === "error" ? pause.error
    : isLoading ? "Reading the authenticated workspace state from the backend."
    : pause.paused ? "No actions can pass the backend policy gateway while paused."
    : "The agent is unpaused. Backend policy and approval checks remain active.";
  return <section aria-label="Global pause control" className="flex flex-wrap items-center gap-3 rounded-xl border border-[#eadfce] bg-[#fffaf3] px-4 py-3.5">
    <div className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-[#f4e8d7] text-[#a77332]">{isLoading ? <LoaderCircle size={15} className="animate-spin"/> : <LockKeyhole size={15}/>}</div>
    <div className="min-w-[180px] flex-1"><div className="text-xs font-semibold text-[#604c31]">{title}</div><p className="mb-0 mt-1 text-[11px] leading-relaxed text-[#88785f]">{detail}</p>{pause.error && <p role="alert" className="mb-0 mt-1 text-[11px] font-medium text-[#aa4d35]">{pause.error}</p>}</div>
    {pause.status === "error" && accessToken && <button onClick={()=>void pause.refresh()} disabled={pause.busy} className="rounded-md border border-[#e7d9c4] bg-white px-3 py-2 text-[10px] font-semibold text-[#765c38] disabled:opacity-60"><RefreshCw size={12}/><span className="sr-only">Reload pause state</span></button>}
    {pause.status === "ready" && <button onClick={()=>void pause.update()} disabled={pause.busy} className="shrink-0 rounded-md border border-[#e7d9c4] bg-white px-3 py-2 text-[11px] font-semibold text-[#765c38] hover:bg-[#fff5e7] disabled:opacity-60">{pause.busy ? "Saving…" : pause.paused ? "Resume" : "Pause"}</button>}
  </section>;
}
