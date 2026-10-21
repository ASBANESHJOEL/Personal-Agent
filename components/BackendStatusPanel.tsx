"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, AlertCircle, CheckCircle2, CircleHelp, LoaderCircle, RefreshCw, Server, WifiOff } from "lucide-react";
import { getAuthenticatedStorageVerification, getBackendStatus, subscribeToAuthenticatedStorageVerification, type BackendStatus } from "../lib/api-client";

const initial: BackendStatus = { state: "loading" };
const badge: Record<Exclude<BackendStatus["state"], "loading">, { label: string; icon: typeof Server; classes: string }> = {
  online: { label: "Online", icon: CheckCircle2, classes: "bg-[#edf5f0] text-green" },
  degraded: { label: "Degraded", icon: AlertCircle, classes: "bg-[#fff7e7] text-[#966c27]" },
  offline: { label: "Offline", icon: WifiOff, classes: "bg-[#fff0ed] text-[#aa4d35]" },
  unknown: { label: "Unknown", icon: CircleHelp, classes: "bg-canvas text-muted" },
};

export default function BackendStatusPanel() {
  const [status, setStatus] = useState<BackendStatus>(initial);
  const [authenticatedStorageVerified, setAuthenticatedStorageVerified] = useState(getAuthenticatedStorageVerification);
  const [checking, setChecking] = useState(false);
  const requestInFlight = useRef(false);
  const mounted = useRef(true);
  const statusRef = useRef<BackendStatus>(initial);
  statusRef.current = status;
  const refresh = useCallback(async () => {
    if (requestInFlight.current) return;
    requestInFlight.current = true; setChecking(true);
    if (statusRef.current.state === "loading") setStatus(initial);
    try {
      const next = await getBackendStatus();
      if (mounted.current) setStatus(next);
    } catch {
      if (mounted.current) setStatus({ state: "unknown", detail: "Backend status could not be verified.", checkedAt: new Date().toISOString() });
    } finally { requestInFlight.current = false; if (mounted.current) setChecking(false); }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const unsubscribe = subscribeToAuthenticatedStorageVerification(setAuthenticatedStorageVerified);
    void refresh();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 30_000);
    return () => { mounted.current = false; window.clearInterval(timer); unsubscribe(); };
  }, [refresh]);

  const view = status.state === "loading" ? null : badge[status.state];
  const StatusIcon = view?.icon ?? LoaderCircle;
  const backendDetail = status.state === "online"
    ? status.readiness.storage === "supabase-postgres"
      ? authenticatedStorageVerified
        ? "API online · Supabase configured; authenticated storage access verified by a signed-in state request."
        : "API online · Supabase configured; authenticated storage access not yet verified."
      : `API ready · persistence: ${status.readiness.storage === "local-sqlite" ? "local SQLite" : status.readiness.storage} (${status.readiness.verified ? "verified" : "not verified"})`
    : status.state === "degraded" && status.readiness
      ? `${status.readiness.storage}: ${status.readiness.configured ? "configured" : "not configured"}, ${status.readiness.verified ? "verified" : "not verified"}. ${status.detail}`
    : status.state === "loading" ? "Checking health and readiness…" : status.detail;
  return <section aria-label="Backend status" className="rounded-2xl border border-line bg-white p-4 shadow-soft md:p-5">
    <div className="flex flex-wrap items-center justify-between gap-3"><div className="flex items-center gap-3"><div className="grid h-9 w-9 place-items-center rounded-xl bg-canvas text-ink"><Server size={17}/></div><div><h2 className="m-0 text-xs font-semibold">Personal Agent API</h2><p className="mb-0 mt-1 text-[10px] text-muted">Live health and readiness checks</p></div><span role="status" aria-label={`Backend ${view?.label ?? "loading"}`} className={`inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-semibold ${view?.classes ?? "bg-canvas text-muted"}`}><StatusIcon size={12} className={checking && status.state === "loading" ? "animate-spin" : ""}/>{view?.label ?? "Loading"}</span></div><button onClick={()=>void refresh()} disabled={checking} className="flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-[10px] font-semibold text-ink hover:border-green/40 disabled:opacity-60"><RefreshCw size={12} className={checking ? "animate-spin" : ""}/>Refresh</button></div>
    <div className="mt-3 flex items-start gap-2 text-[11px] text-muted"><Activity size={13} className="mt-0.5 shrink-0"/><span>{backendDetail}</span></div>
    <p className="mb-0 mt-2 border-t border-line pt-2 text-[10px] leading-relaxed text-muted">This reports the FastAPI service and configured persistence check. It does not indicate Supabase Auth or any social platform is connected.</p>
  </section>;
}
