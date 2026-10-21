"use client";

import { useEffect, useMemo, useState } from "react";
import { ArrowUpRight, RefreshCw, Search } from "lucide-react";
import { ApiError, getDiscovery, runDiscovery, updateDiscoveryStatus, type DiscoveryItem, type DiscoveryPlatform, type DiscoveryResponse, type DiscoverySourceResult } from "../lib/api-client";

type Props = { accessToken: string | null };
type SortMode = "ranked" | "recent" | "popular";

export default function DiscoveryPanel({ accessToken }: Props) {
  const [items, setItems] = useState<DiscoveryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [platforms, setPlatforms] = useState<DiscoveryPlatform[]>(["github", "huggingface"]);
  const [hfTypes, setHfTypes] = useState<Array<"model" | "dataset" | "space">>(["model", "dataset", "space"]);
  // The active feed contains only proposed discoveries. Saved and dismissed
  // records remain available through the status filter.
  const [statusFilter, setStatusFilter] = useState<"all" | DiscoveryItem["status"]>("proposed");
  const [sort, setSort] = useState<SortMode>("ranked");
  const [sources, setSources] = useState<DiscoverySourceResult[]>([]);
  const [lastRun, setLastRun] = useState("");
  const [page, setPage] = useState(1);

  useEffect(() => {
    let active = true;
    setItems([]); setSources([]); setError("");
    if (!accessToken) { setLoading(false); return; }
    setLoading(true);
    void getDiscovery(accessToken).then((result) => { if (active) setItems(result); })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [accessToken]);

  const visible = useMemo(() => {
    const matched = items.filter((item) => (statusFilter === "all" || item.status === statusFilter) &&
      (platforms.includes(item.platform)) && `${item.title} ${item.description} ${JSON.stringify(item.metadata)}`.toLowerCase().includes(query.trim().toLowerCase()));
    return [...matched].sort((a, b) => sort === "ranked" ? b.ranking_score - a.ranking_score : sort === "popular" ? popularity(b) - popularity(a) : dateValue(b) - dateValue(a));
  }, [items, platforms, query, sort, statusFilter]);

  async function discover(targetPage = page) {
    if (!accessToken || running || platforms.length === 0 || (platforms.includes("huggingface") && hfTypes.length === 0)) return;
    setRunning(true); setError(""); setSources([]);
    try {
      const result: DiscoveryResponse = await runDiscovery(accessToken, { query: query.trim(), platforms, hf_types: hfTypes, limit: 30, page: targetPage });
      setItems(result.items); setSources(result.sources); setLastRun(new Date().toLocaleTimeString());
      if (!result.sources.some((source) => source.status === "ok")) setError(result.sources.map((source) => `${source.source}: ${source.detail ?? source.status}`).join(" ") || "No providers returned results.");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setRunning(false); }
  }

  async function changeStatus(item: DiscoveryItem, status: "saved" | "dismissed") {
    if (!accessToken || busyId) return;
    setBusyId(item.id); setError("");
    try {
      const updated = await updateDiscoveryStatus(accessToken, item.id, status);
      setItems((current) => current.map((entry) => entry.id === item.id ? updated : entry));
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusyId(null); }
  }

  function togglePlatform(platform: DiscoveryPlatform) {
    setPage(1);
    setPlatforms((current) => current.includes(platform) ? current.filter((item) => item !== platform) : [...current, platform]);
  }

  function toggleHfType(kind: "model" | "dataset" | "space") {
    setPage(1);
    setHfTypes((current) => current.includes(kind) ? current.filter((item) => item !== kind) : [...current, kind]);
  }

  return <div className="space-y-5">
    <section className="rounded-2xl border border-line bg-white p-4 shadow-soft md:p-5" aria-label="Discovery controls">
      <div className="grid gap-3 md:grid-cols-[1fr_auto]">
        <label className="flex items-center gap-2 rounded-lg border border-line bg-canvas px-3"><Search size={15} className="shrink-0 text-muted"/><input aria-label="Search discovery results" value={query} onChange={(event) => { setQuery(event.target.value); setPage(1); setError(""); }} placeholder="Search ML, vision, robotics, research…" className="h-10 min-w-0 flex-1 bg-transparent text-xs outline-none"/></label>
        <button onClick={() => void discover()} disabled={!accessToken || running || platforms.length === 0 || (platforms.includes("huggingface") && hfTypes.length === 0)} className="inline-flex items-center justify-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-xs font-semibold text-white disabled:cursor-not-allowed disabled:opacity-50"><RefreshCw size={14} className={running ? "animate-spin" : ""}/>{running ? "Searching public sources…" : "Discover now"}</button>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-5 gap-y-3 text-xs">
        <span className="font-semibold text-ink">Sources</span>
        <label className="flex items-center gap-2"><input type="checkbox" checked={platforms.includes("github")} onChange={() => togglePlatform("github")}/>GitHub repositories</label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={platforms.includes("huggingface")} onChange={() => togglePlatform("huggingface")}/>Hugging Face</label>
        {platforms.includes("huggingface") && <span className="flex flex-wrap gap-3 border-l border-line pl-4">{(["model", "dataset", "space"] as const).map((kind) => <label key={kind} className="flex items-center gap-1.5"><input type="checkbox" checked={hfTypes.includes(kind)} onChange={() => toggleHfType(kind)}/>{kind[0].toUpperCase() + kind.slice(1)}s</label>)}</span>}
        <label className="ml-auto flex items-center gap-2 text-muted">Status<select aria-label="Filter discovery status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)} className="rounded-md border border-line bg-white px-2 py-1 text-ink"><option value="all">All</option><option value="proposed">New</option><option value="saved">Saved</option><option value="dismissed">Dismissed</option></select></label>
        <label className="flex items-center gap-2 text-muted">Sort<select aria-label="Sort discovery results" value={sort} onChange={(event) => setSort(event.target.value as SortMode)} className="rounded-md border border-line bg-white px-2 py-1 text-ink"><option value="ranked">Best match</option><option value="recent">Recently updated</option><option value="popular">Popularity metadata</option></select></label>
        <div className="ml-auto flex items-center gap-2"><button aria-label="Previous provider page" disabled={page <= 1 || running} onClick={() => { const next = page - 1; setPage(next); void discover(next); }} className="rounded border border-line px-2 py-1 disabled:opacity-40">Previous</button><span className="text-muted">Page {page}</span><button aria-label="Next provider page" disabled={running} onClick={() => { const next = page + 1; setPage(next); void discover(next); }} className="rounded border border-line px-2 py-1 disabled:opacity-40">Next</button></div>
      </div>
      {!accessToken && <p role="status" className="mb-0 mt-4 rounded-lg border border-[#ecd9ae] bg-[#fff9ec] p-3 text-xs text-[#805e21]">Sign in with Supabase Auth to search and save discoveries. Results are never simulated.</p>}
      {error && <p role="alert" className="mb-0 mt-4 rounded-lg border border-[#f0cfc8] bg-[#fff5f3] p-3 text-xs text-[#963e2e]">{error}</p>}
    </section>

    {sources.length > 0 && <div aria-label="Source run summary" className="flex flex-wrap gap-2">{sources.map((source) => <span key={source.source} className={`rounded-md px-2.5 py-1.5 text-[10px] ${source.status === "ok" ? "bg-[#edf5f0] text-green" : "bg-[#fff4e6] text-[#896018]"}`}>{source.source}: {source.status === "ok" ? `${source.count} found` : source.detail ?? source.status}</span>)}</div>}
    {lastRun && <p className="-mt-3 mb-0 text-[10px] text-muted">Last completed search: {lastRun}. Provider metadata and match scores are informational, not verified quality measures.</p>}
    {loading ? <div role="status" className="grid min-h-[220px] place-items-center rounded-2xl border border-line bg-white text-sm text-muted">Loading your saved discovery results…</div>
      : visible.length === 0 ? <div className="grid min-h-[240px] place-items-center rounded-2xl border border-dashed border-[#dce4df] bg-white px-6 text-center"><div className="max-w-sm"><div className="text-xs font-semibold text-ink">{items.length ? "No results match these filters" : lastRun ? "No results from this search" : "No discoveries yet"}</div><p className="mb-0 mt-2 text-xs leading-relaxed text-muted">{items.length ? "Try changing the search, source, or status filters." : lastRun ? "No new records were returned by the selected providers. Try another query or check the source status above." : "Run a search to retrieve public GitHub and Hugging Face metadata. Only records returned by those APIs will appear here."}</p></div></div>
      : <div className="grid gap-4 xl:grid-cols-2">{visible.map((item) => <DiscoveryCard key={item.id} item={item} busy={busyId === item.id} onStatus={changeStatus}/>)}</div>}
    {!error && sources.some((source) => source.status !== "ok") && sources.some((source) => source.status === "ok") && <p role="status" className="text-xs text-[#896018]">Some sources failed or were rate limited. Results above contain only successful provider responses.</p>}
  </div>;
}

function DiscoveryCard({ item, busy, onStatus }: { item: DiscoveryItem; busy: boolean; onStatus: (item: DiscoveryItem, status: "saved" | "dismissed") => void }) {
  const metadata = item.metadata;
  const tags = arrayOfStrings(metadata.topics ?? metadata.tags).slice(0, 5);
  const metric = item.platform === "github" ? numberValue(metadata.stars) : numberValue(metadata.downloads);
  const date = typeof metadata.updated_at === "string" ? metadata.updated_at : typeof metadata.last_modified === "string" ? metadata.last_modified : "";
  return <article className="rounded-2xl border border-line bg-white p-5 shadow-soft">
    <div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="rounded bg-[#edf5f0] px-2 py-1 text-[9px] font-bold uppercase tracking-wider text-green">{item.platform === "github" ? "GitHub repository" : `Hugging Face ${item.kind}`}</span><span className="text-[10px] text-muted">Match {item.ranking_score}/100</span><StatusBadge status={item.status}/></div><h2 className="mb-0 mt-3 break-words text-sm font-semibold">{item.title}</h2></div></div>
    {item.description && <p className="mb-0 mt-2 whitespace-pre-wrap text-xs leading-relaxed text-[#65736d]">{item.description}</p>}
    <div className="mt-3 flex flex-wrap gap-1.5">{tags.map((tag) => <span key={tag} className="rounded bg-canvas px-2 py-1 text-[9px] text-muted">{tag}</span>)}{metric !== null && <span className="rounded bg-canvas px-2 py-1 text-[9px] text-muted">{item.platform === "github" ? `${metric.toLocaleString()} stars reported` : `${metric.toLocaleString()} downloads reported`}</span>}{date && <span className="rounded bg-canvas px-2 py-1 text-[9px] text-muted">Updated {date.slice(0, 10)}</span>}</div>
    <ul className="mb-0 mt-4 space-y-1.5 border-t border-line pt-3">{item.ranking_reasons.map((reason) => <li key={reason} className="text-[11px] leading-relaxed text-muted">• {reason}</li>)}</ul>
    <div className="mt-4 flex flex-wrap items-center justify-between gap-2"><a href={item.source_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[11px] font-semibold text-green hover:underline">Open source <ArrowUpRight size={13}/></a><div className="flex gap-2"><button disabled={busy || item.status === "dismissed"} onClick={() => onStatus(item, "dismissed")} className="rounded-lg border border-line px-3 py-2 text-[10px] font-semibold text-muted disabled:opacity-50">Dismiss</button><button disabled={busy || item.status === "saved"} onClick={() => onStatus(item, "saved")} className="rounded-lg bg-ink px-3 py-2 text-[10px] font-semibold text-white disabled:opacity-50">{busy ? "Saving…" : item.status === "saved" ? "Saved" : "Save"}</button></div></div>
  </article>;
}

function StatusBadge({ status }: { status: DiscoveryItem["status"] }) { return <span className="rounded bg-canvas px-2 py-1 text-[9px] font-semibold capitalize text-muted">{status}</span>; }
function arrayOfStrings(value: unknown): string[] { return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []; }
function numberValue(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function popularity(item: DiscoveryItem): number { return numberValue(item.metadata.stars) ?? numberValue(item.metadata.downloads) ?? 0; }
function dateValue(item: DiscoveryItem): number { const raw = item.metadata.updated_at ?? item.metadata.last_modified; return typeof raw === "string" ? Date.parse(raw) || 0 : 0; }
function errorMessage(reason: unknown): string {
  if (reason instanceof ApiError && reason.status === 401) return "Your session is missing or expired. Sign in again to access discovery.";
  if (reason instanceof ApiError && reason.status === 403) return "The backend denied access to your discovery records.";
  if (reason instanceof ApiError && reason.kind === "network") return "Could not reach the backend. Start FastAPI and check its CORS configuration.";
  if (reason instanceof ApiError && reason.kind === "timeout") return "The discovery request timed out. Try again with a narrower query.";
  return reason instanceof Error ? reason.message : "Discovery could not be completed.";
}
