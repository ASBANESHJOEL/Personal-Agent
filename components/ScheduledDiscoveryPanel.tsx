"use client";

import { useEffect, useState } from "react";
import { CalendarClock, RefreshCw } from "lucide-react";
import { ApiError, getDiscoverySchedule, updateDiscoverySchedule, type DiscoverySchedule, type ScheduledRun } from "../lib/api-client";

export default function ScheduledDiscoveryPanel({ accessToken, mode = "settings" }: { accessToken: string | null; mode?: "settings" | "activity" }) {
  const [schedule, setSchedule] = useState<DiscoverySchedule | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  async function load(token: string) {
    setLoading(true);
    setError("");
    try { setSchedule(await getDiscoverySchedule(token)); }
    catch (reason) { setSchedule(null); setError(message(reason)); }
    finally { setLoading(false); }
  }

  useEffect(() => {
    setSchedule(null);
    setError("");
    if (!accessToken) { setLoading(false); return; }
    void load(accessToken);
  }, [accessToken]);

  async function toggle() {
    if (!accessToken || !schedule || saving || !schedule.configured) return;
    setSaving(true);
    setError("");
    try { setSchedule(await updateDiscoverySchedule(accessToken, !schedule.enabled)); }
    catch (reason) { setError(message(reason)); }
    finally { setSaving(false); }
  }

  if (!accessToken) return <Panel mode={mode}><p role="status" className="mb-0 text-xs text-muted">Sign in to view or change your personal discovery schedule.</p></Panel>;
  if (loading && !schedule) return <Panel mode={mode}><p role="status" className="mb-0 flex items-center gap-2 text-xs text-muted"><RefreshCw size={14} className="animate-spin"/>Loading schedule and recent runs…</p></Panel>;
  if (error && !schedule) return <Panel mode={mode}><p role="alert" className="mb-0 text-xs text-[#963e2e]">{error}</p><button onClick={() => void load(accessToken)} className="mt-3 rounded-lg border border-line px-3 py-2 text-[11px] font-semibold">Retry</button></Panel>;
  if (!schedule) return null;

  if (mode === "activity") return <Panel mode={mode}>
    <h2 className="m-0 flex items-center gap-2 text-sm font-semibold"><CalendarClock size={16}/>Scheduled discovery runs</h2>
    {!schedule.configured && <p role="status" className="mb-0 mt-3 text-xs text-[#805e21]">Cloud scheduling is not configured. Apply the scheduling migration and configure Supabase storage to load run history.</p>}
    {schedule.configured && !schedule.worker_deployed && <p role="status" className="mb-0 mt-3 text-xs text-[#805e21]">The Railway Cron worker is not marked as deployed.</p>}
    {error && <p role="alert" className="mb-0 mt-3 text-xs text-[#963e2e]">{error}</p>}
    <RunHistory runs={schedule.recent_runs}/>
    {!schedule.recent_runs.length && <p className="mb-0 mt-3 text-xs text-muted">No scheduled runs have been recorded for your account.</p>}
    <button onClick={() => void load(accessToken)} disabled={loading} className="mt-4 rounded-lg border border-line px-3 py-2 text-[11px] font-semibold disabled:opacity-50">{loading ? "Refreshing…" : "Refresh run history"}</button>
  </Panel>;

  return <Panel mode={mode}>
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><h2 className="m-0 flex items-center gap-2 text-sm font-semibold"><CalendarClock size={16}/>Daily discovery</h2>
        <p className="mb-0 mt-2 text-xs leading-relaxed text-muted">Runs once daily at {String(schedule.schedule_hour_utc).padStart(2, "0")}:00 UTC (08:30 India Standard Time at the default 03:00 UTC schedule). Only public metadata is collected; no platform actions are performed.</p>
      </div>
      <button onClick={() => void toggle()} disabled={!schedule.configured || saving} aria-pressed={schedule.enabled} className="rounded-lg bg-ink px-4 py-2.5 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50">
        {saving ? "Saving…" : schedule.enabled ? "Disable daily discovery" : "Enable daily discovery"}
      </button>
    </div>
    {!schedule.configured && <p role="status" className="mb-0 mt-4 rounded-lg border border-[#ecd9ae] bg-[#fff9ec] p-3 text-xs text-[#805e21]">Cloud scheduling is not configured. Set the API service to use Supabase storage and apply the scheduling migration before enabling this setting.</p>}
    {schedule.configured && !schedule.worker_deployed && <p role="status" className="mb-0 mt-4 rounded-lg border border-[#ecd9ae] bg-[#fff9ec] p-3 text-xs text-[#805e21]">The preference can be saved, but a Railway Cron worker is not marked as deployed. No runs will happen until the worker service is configured.</p>}
    {schedule.detail && schedule.configured && schedule.worker_deployed && <p role="status" className="mb-0 mt-4 text-xs text-muted">{schedule.detail}</p>}
    {error && <p role="alert" className="mb-0 mt-4 rounded-lg border border-[#f0cfc8] bg-[#fff5f3] p-3 text-xs text-[#963e2e]">{error}</p>}
    <dl className="mb-0 mt-4 grid gap-3 border-t border-line pt-4 text-xs sm:grid-cols-3">
      <div><dt className="text-muted">Schedule</dt><dd className="mb-0 mt-1 font-medium">{schedule.enabled ? "Next: " + displayDate(schedule.next_scheduled_at) : "Disabled"}</dd></div>
      <div><dt className="text-muted">Last run</dt><dd className="mb-0 mt-1 font-medium">{schedule.recent_runs[0] ? displayDate(schedule.recent_runs[0].started_at) : "No runs yet"}</dd></div>
      <div><dt className="text-muted">Latest result</dt><dd className="mb-0 mt-1 font-medium">{schedule.recent_runs[0] ? statusLabel(schedule.recent_runs[0]) : "Waiting for first scheduled run"}</dd></div>
    </dl>
    {schedule.recent_runs[0]?.error_summary && <p role="status" className="mb-0 mt-3 text-xs text-[#963e2e]">{schedule.recent_runs[0].error_summary}</p>}
    {schedule.recent_runs.length > 1 && <RunHistory runs={schedule.recent_runs.slice(1, 4)}/>}
  </Panel>;
}

function Panel({ children, mode }: { children: React.ReactNode; mode: "settings" | "activity" }) {
  return <section aria-label={mode === "settings" ? "Scheduled discovery settings" : "Scheduled discovery activity"} className="rounded-2xl border border-line bg-white p-5 shadow-soft">{children}</section>;
}

function RunHistory({ runs }: { runs: ScheduledRun[] }) {
  return <ul className="mb-0 mt-4 space-y-2">{runs.map((run) => <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3 text-xs">
    <span>{displayDate(run.started_at)} · {statusLabel(run)}</span>
    <span className="text-muted">+{run.new_discoveries} new · {run.updated_discoveries} updated · {run.attempt_count} attempt{run.attempt_count === 1 ? "" : "s"}</span>
    {run.error_summary && <span className="w-full text-[#963e2e]">{run.error_summary}</span>}
  </li>)}</ul>;
}

function statusLabel(run: ScheduledRun): string {
  const okSources = run.source_results.filter((source) => source.status === "ok").length;
  const failedSources = run.source_results.length - okSources;
  const status = run.status.replaceAll("_", " ");
  return run.status === "succeeded" || run.status === "partially_failed"
    ? status + " · " + okSources + " source(s) succeeded, " + failedSources + " failed"
    : status;
}

function displayDate(value: string | null): string {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Time unavailable" : date.toLocaleString() + " (" + date.toISOString().slice(0, 16).replace("T", " ") + " UTC)";
}

function message(reason: unknown): string {
  if (reason instanceof ApiError && reason.status === 401) return "Your session is missing or expired. Sign in again to manage scheduling.";
  if (reason instanceof ApiError && reason.status === 403) return "The backend denied access to this account's schedule.";
  if (reason instanceof ApiError && reason.kind === "network") return "Could not reach the backend. Check that FastAPI is running.";
  if (reason instanceof ApiError && reason.kind === "timeout") return "The schedule request timed out. Try again.";
  return reason instanceof Error ? reason.message : "Could not load or update scheduled discovery.";
}
