export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
const REQUEST_TIMEOUT_MS = 5_000;
const DISCOVERY_TIMEOUT_MS = 35_000;
const SCHEDULE_TIMEOUT_MS = 15_000;

export type ApiErrorKind = "timeout" | "network" | "http" | "invalid-response";
export class ApiError extends Error {
  constructor(message: string, readonly kind: ApiErrorKind, readonly status?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export type HealthResponse = { status: "ok" };
export type ReadinessResponse = { status: "ready" | "configured" | "not_ready"; storage: string; configured: boolean; verified: boolean; detail?: string };
export type AgentStateResponse = { paused: boolean };
export type DiscoveryPlatform = "github" | "huggingface";
export type DiscoveryItem = {
  id: string; platform: DiscoveryPlatform; kind: string; source_identity: string; source_url: string;
  title: string; description: string; metadata: Record<string, unknown>; ranking_score: number;
  ranking_reasons: string[]; status: "proposed" | "saved" | "dismissed"; created_at?: string | null;
};
export type DiscoverySourceResult = { source: string; status: "ok" | "rate_limited" | "error" | "timeout"; count: number; detail?: string | null };
export type DiscoveryResponse = { items: DiscoveryItem[]; sources: DiscoverySourceResult[]; query: string };
export type ScheduledRun = {
  id: string; status: string; started_at: string | null; finished_at: string | null;
  source_results: DiscoverySourceResult[]; new_discoveries: number; updated_discoveries: number;
  error_summary: string | null; attempt_count: number;
};
export type DiscoverySchedule = {
  configured: boolean; worker_deployed: boolean; enabled: boolean; schedule_hour_utc: number;
  timezone: "UTC"; next_scheduled_at: string | null; recent_runs: ScheduledRun[]; detail?: string | null;
};
export type BackendStatus =
  | { state: "loading" }
  | { state: "online"; health: HealthResponse; readiness: ReadinessResponse; checkedAt: string }
  | { state: "degraded"; health: HealthResponse | null; readiness: ReadinessResponse | null; detail: string; checkedAt: string }
  | { state: "offline"; detail: string; checkedAt: string }
  | { state: "unknown"; detail: string; checkedAt: string };

let authenticatedStorageVerified = false;
const storageVerificationListeners = new Set<(verified: boolean) => void>();

export function getAuthenticatedStorageVerification(): boolean {
  return authenticatedStorageVerified;
}

export function setAuthenticatedStorageVerification(verified: boolean): void {
  if (authenticatedStorageVerified === verified) return;
  authenticatedStorageVerified = verified;
  storageVerificationListeners.forEach((listener) => listener(verified));
}

export function subscribeToAuthenticatedStorageVerification(listener: (verified: boolean) => void): () => void {
  storageVerificationListeners.add(listener);
  listener(authenticatedStorageVerified);
  return () => { storageVerificationListeners.delete(listener); };
}

async function requestJson<T>(path: string, options: { token?: string; method?: "GET" | "POST" | "DELETE" | "PATCH"; body?: unknown; timeoutMs?: number } = {}): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), options.timeoutMs ?? REQUEST_TIMEOUT_MS);
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.token) headers.Authorization = `Bearer ${options.token}`;
  try {
    let response: Response;
    try {
      response = await fetch(`${API_BASE_URL}${path}`, {
        method: options.method ?? "GET", headers, signal: controller.signal,
        ...(options.body === undefined ? {} : { body: JSON.stringify(options.body) }),
      });
    } catch (error) {
      if (controller.signal.aborted) throw new ApiError("The backend request timed out.", "timeout");
      throw new ApiError("Could not reach the backend. Check that the API is running.", "network");
    }
    if (!response.ok) {
      let detail = `Backend returned HTTP ${response.status}.`;
      try {
        const payload = await response.json();
        if (typeof payload?.detail === "string") detail = payload.detail;
        else if (typeof payload?.detail?.reason === "string") detail = payload.detail.reason.replaceAll("_", " ");
      } catch { /* Keep the status-based message. */ }
      throw new ApiError(detail, "http", response.status);
    }
    try { return await response.json() as T; }
    catch { throw new ApiError("Backend returned an invalid JSON response.", "invalid-response", response.status); }
  } finally {
    window.clearTimeout(timer);
  }
}

export async function getHealth(): Promise<HealthResponse> {
  const value = await requestJson<unknown>("/health");
  if (!isRecord(value) || value.status !== "ok") throw new ApiError("Health response did not match the API schema.", "invalid-response");
  return { status: "ok" };
}

export async function getReadiness(): Promise<ReadinessResponse> {
  const value = await requestJson<unknown>("/ready");
  if (!isRecord(value) || !["ready", "configured", "not_ready"].includes(String(value.status)) || typeof value.storage !== "string" || typeof value.configured !== "boolean" || typeof value.verified !== "boolean" || (value.detail !== undefined && typeof value.detail !== "string")) throw new ApiError("Readiness response did not match the API schema.", "invalid-response");
  return { status: value.status as ReadinessResponse["status"], storage: value.storage, configured: value.configured, verified: value.verified, ...(typeof value.detail === "string" ? { detail: value.detail } : {}) };
}

export async function getAgentState(token: string): Promise<AgentStateResponse> {
  const value = await requestJson<unknown>("/api/v1/state", { token });
  if (!isRecord(value) || typeof value.paused !== "boolean") throw new ApiError("Agent state response did not match the API schema.", "invalid-response");
  return { paused: value.paused };
}

export async function setAgentPaused(token: string, paused: boolean): Promise<AgentStateResponse> {
  const value = await requestJson<unknown>("/api/v1/state/pause", { token, method: paused ? "POST" : "DELETE" });
  if (!isRecord(value) || value.paused !== paused) throw new ApiError("Pause response did not confirm the requested state.", "invalid-response");
  return { paused };
}

export async function getDiscovery(token: string, status?: DiscoveryItem["status"]): Promise<DiscoveryItem[]> {
  const path = `/api/v1/discovery${status ? `?status=${encodeURIComponent(status)}` : ""}`;
  const value = await requestJson<unknown>(path, { token });
  if (!Array.isArray(value) || !value.every(isDiscoveryItem)) throw new ApiError("Discovery response did not match the API schema.", "invalid-response");
  return value;
}

export async function runDiscovery(token: string, input: { query: string; platforms: DiscoveryPlatform[]; hf_types: Array<"model" | "dataset" | "space">; limit: number; page: number }): Promise<DiscoveryResponse> {
  const value = await requestJson<unknown>("/api/v1/discovery/run", { token, method: "POST", body: input, timeoutMs: DISCOVERY_TIMEOUT_MS });
  if (!isRecord(value) || !Array.isArray(value.items) || !value.items.every(isDiscoveryItem) || !Array.isArray(value.sources) || !value.sources.every(isDiscoverySource) || typeof value.query !== "string") {
    throw new ApiError("Discovery run response did not match the API schema.", "invalid-response");
  }
  return value as DiscoveryResponse;
}

export async function updateDiscoveryStatus(token: string, itemId: string, status: "saved" | "dismissed"): Promise<DiscoveryItem> {
  const value = await requestJson<unknown>(`/api/v1/discovery/${encodeURIComponent(itemId)}`, { token, method: "PATCH", body: { status } });
  if (!isDiscoveryItem(value) || value.status !== status) throw new ApiError("Discovery status response did not confirm the requested update.", "invalid-response");
  return value;
}

export async function getDiscoverySchedule(token: string): Promise<DiscoverySchedule> {
  const value = await requestJson<unknown>("/api/v1/discovery/schedule", { token, timeoutMs: SCHEDULE_TIMEOUT_MS });
  if (!isDiscoverySchedule(value)) throw new ApiError("Discovery schedule response did not match the API schema.", "invalid-response");
  return value;
}

export async function updateDiscoverySchedule(token: string, enabled: boolean): Promise<DiscoverySchedule> {
  const value = await requestJson<unknown>("/api/v1/discovery/schedule", { token, method: "POST", body: { enabled }, timeoutMs: SCHEDULE_TIMEOUT_MS });
  if (!isDiscoverySchedule(value) || value.enabled !== enabled) throw new ApiError("Discovery schedule response did not confirm the requested setting.", "invalid-response");
  return value;
}

function isDiscoverySchedule(value: unknown): value is DiscoverySchedule {
  return isRecord(value) && typeof value.configured === "boolean" && typeof value.worker_deployed === "boolean"
    && typeof value.enabled === "boolean" && Number.isInteger(value.schedule_hour_utc)
    && value.timezone === "UTC" && (value.next_scheduled_at === null || typeof value.next_scheduled_at === "string")
    && Array.isArray(value.recent_runs) && value.recent_runs.every((run) => isRecord(run)
      && typeof run.id === "string" && typeof run.status === "string"
      && (run.started_at === null || typeof run.started_at === "string")
      && (run.finished_at === null || typeof run.finished_at === "string")
      && Array.isArray(run.source_results) && run.source_results.every(isDiscoverySource)
      && Number.isInteger(run.new_discoveries) && Number.isInteger(run.updated_discoveries)
      && (run.error_summary === null || typeof run.error_summary === "string")
      && Number.isInteger(run.attempt_count))
    && (value.detail === undefined || value.detail === null || typeof value.detail === "string");
}

export async function getBackendStatus(): Promise<BackendStatus> {
  const [healthResult, readinessResult] = await Promise.allSettled([getHealth(), getReadiness()]);
  const health = healthResult.status === "fulfilled" ? healthResult.value : null;
  const readiness = readinessResult.status === "fulfilled" ? readinessResult.value : null;
  const checkedAt = new Date().toISOString();
  if (health && readiness && readiness.status !== "not_ready" && readiness.configured) return { state: "online", health, readiness, checkedAt };
  const errors = [healthResult, readinessResult].filter((r): r is PromiseRejectedResult => r.status === "rejected").map((r) => r.reason);
  const detail = errors.map((error) => error instanceof Error ? error.message : "Backend status could not be verified.").join(" ");
  if (health || readiness || errors.some((e) => e instanceof ApiError && e.kind === "http")) {
    const degradedDetail = errors.length
      ? detail
      : readiness?.detail ?? (readiness ? `Storage is ${readiness.configured ? "configured" : "not configured"} and ${readiness.verified ? "verified" : "not verified"}.` : "Backend readiness could not be verified.");
    return { state: "degraded", health, readiness, detail: degradedDetail, checkedAt };
  }
  if (errors.length && errors.every((e) => e instanceof ApiError && ["network", "timeout"].includes(e.kind))) {
    return { state: "offline", detail, checkedAt };
  }
  return { state: "unknown", detail, checkedAt };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isDiscoveryItem(value: unknown): value is DiscoveryItem {
  return isRecord(value) && typeof value.id === "string" && (value.platform === "github" || value.platform === "huggingface") && typeof value.kind === "string" &&
    typeof value.source_identity === "string" && typeof value.source_url === "string" && typeof value.title === "string" && typeof value.description === "string" &&
    isRecord(value.metadata) && typeof value.ranking_score === "number" && Array.isArray(value.ranking_reasons) && value.ranking_reasons.every((reason) => typeof reason === "string") &&
    ["proposed", "saved", "dismissed"].includes(String(value.status));
}

function isDiscoverySource(value: unknown): value is DiscoverySourceResult {
  return isRecord(value) && typeof value.source === "string" && ["ok", "rate_limited", "error", "timeout"].includes(String(value.status)) && typeof value.count === "number" &&
    (value.detail === undefined || value.detail === null || typeof value.detail === "string");
}
