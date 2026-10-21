import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, getBackendStatus, getDiscoverySchedule, getHealth, getReadiness, setAuthenticatedStorageVerification, updateDiscoverySchedule, updateDiscoveryStatus, type DiscoveryItem } from "./api-client";

function response(value: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => value } as Response;
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  setAuthenticatedStorageVerification(false);
});

describe("typed backend client", () => {
  it("uses the signed-in token for schedule reads and changes without accepting a browser-supplied user id", async () => {
    const schedule = { configured: true, worker_deployed: true, enabled: false, schedule_hour_utc: 3, timezone: "UTC", next_scheduled_at: null, recent_runs: [] };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(schedule))
      .mockResolvedValueOnce(response({ ...schedule, enabled: true, next_scheduled_at: "2026-10-21T03:00:00Z" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getDiscoverySchedule("verified-token")).resolves.toEqual(schedule);
    await expect(updateDiscoverySchedule("verified-token", true)).resolves.toMatchObject({ enabled: true });
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://localhost:8000/api/v1/discovery/schedule", expect.objectContaining({
      method: "GET", headers: expect.objectContaining({ Authorization: "Bearer verified-token" }),
    }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://localhost:8000/api/v1/discovery/schedule", expect.objectContaining({
      method: "POST", headers: expect.objectContaining({ Authorization: "Bearer verified-token" }), body: JSON.stringify({ enabled: true }),
    }));
    expect(fetchMock.mock.calls[1][1]).not.toHaveProperty("user_id");
  });

  it("sends an authenticated PATCH for the selected discovery and validates dismissal confirmation", async () => {
    const item: DiscoveryItem = { id: "item-1", platform: "github", kind: "repository", source_identity: "org/repo", source_url: "https://github.com/org/repo", title: "org/repo", description: "", metadata: {}, ranking_score: 10, ranking_reasons: [], status: "dismissed" };
    const fetchMock = vi.fn().mockResolvedValue(response(item));
    vi.stubGlobal("fetch", fetchMock);

    await expect(updateDiscoveryStatus("user-access-token", "item-1", "dismissed")).resolves.toEqual(item);
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/v1/discovery/item-1", expect.objectContaining({
      method: "PATCH",
      headers: expect.objectContaining({ Authorization: "Bearer user-access-token", "Content-Type": "application/json" }),
      body: JSON.stringify({ status: "dismissed" }),
    }));
  });

  it("validates the actual health and readiness response schemas", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response({ status: "ok" })).mockResolvedValueOnce(response({ status: "ready", storage: "local-sqlite", configured: true, verified: true }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await getHealth()).toEqual({ status: "ok" });
    expect(await getReadiness()).toEqual({ status: "ready", storage: "local-sqlite", configured: true, verified: true });
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual(["http://localhost:8000/health", "http://localhost:8000/ready"]);
  });

  it("distinguishes online, degraded, offline, and unknown backend states", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "ok" })).mockResolvedValueOnce(response({ status: "ready", storage: "local-sqlite", configured: true, verified: true }))
      .mockResolvedValueOnce(response({ status: "ok" })).mockResolvedValueOnce(response({ status: "not_ready", storage: "supabase-postgres", configured: true, verified: false, detail: "Data API offline" }))
      .mockRejectedValueOnce(new TypeError("offline")).mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(response({ status: "bad" })).mockResolvedValueOnce(response({ status: "nope" }));
    vi.stubGlobal("fetch", fetchMock);
    expect((await getBackendStatus()).state).toBe("online");
    expect((await getBackendStatus()).state).toBe("degraded");
    expect((await getBackendStatus()).state).toBe("offline");
    expect((await getBackendStatus()).state).toBe("unknown");
  });

  it("keeps a configured but unverified Supabase backend online without claiming storage verification", async () => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(response({ status: "ok" }))
      .mockResolvedValueOnce(response({ status: "configured", storage: "supabase-postgres", configured: true, verified: false })));

    const status = await getBackendStatus();
    expect(status.state).toBe("online");
    if (status.state === "online") {
      expect(status.readiness.configured).toBe(true);
      expect(status.readiness.verified).toBe(false);
    }
  });

  it("reports API HTTP errors without treating them as unreachable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ detail: "not ready" }, 503)));
    await expect(getReadiness()).rejects.toMatchObject<ApiError>({ kind: "http", status: 503, message: "not ready" });
  });

  it("uses an abort timeout and reports health route failures", async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce((_url: string, options: RequestInit) => new Promise((_resolve, reject) => {
        options.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }))
      .mockResolvedValueOnce(response({ detail: "health unavailable" }, 503))
      .mockResolvedValueOnce(response({ status: "ready", storage: "local-sqlite", configured: true, verified: true }));
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    const pendingHealth = getHealth();
    const timeoutAssertion = expect(pendingHealth).rejects.toMatchObject({ kind: "timeout" });
    await vi.advanceTimersByTimeAsync(5_000);
    await timeoutAssertion;
    vi.useRealTimers();
    expect((await getBackendStatus()).state).toBe("degraded");
  });
});
