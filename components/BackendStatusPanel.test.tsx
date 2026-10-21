import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setAuthenticatedStorageVerification } from "../lib/api-client";
import BackendStatusPanel from "./BackendStatusPanel";
import GlobalPauseControl from "./GlobalPauseControl";

function response(value: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => value } as Response;
}

beforeEach(() => setAuthenticatedStorageVerification(false));
afterEach(() => {
  setAuthenticatedStorageVerification(false);
  vi.unstubAllGlobals();
});

describe("backend status panel", () => {
  it("keeps configured Supabase neutral until an authenticated state request succeeds", async () => {
    let resolveState!: (value: Response) => void;
    const stateResponse = new Promise<Response>((resolve) => { resolveState = resolve; });
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/health")) return Promise.resolve(response({ status: "ok" }));
      if (url.endsWith("/ready")) return Promise.resolve(response({
        status: "configured", storage: "supabase-postgres", configured: true, verified: false,
        detail: "Supabase is configured; authenticated storage access has not yet been verified.",
      }));
      if (url.endsWith("/api/v1/state")) return stateResponse;
      return Promise.resolve(response({ detail: "Unexpected request" }, 404));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<>
      <BackendStatusPanel />
      <GlobalPauseControl accessToken="signed-in-user-token" />
    </>);

    expect(await screen.findByText(/Supabase configured; authenticated storage access not yet verified/i)).toBeInTheDocument();
    expect(screen.queryByText(/access verified by a signed-in state request/i)).not.toBeInTheDocument();

    await act(async () => { resolveState(response({ paused: true })); });
    expect(await screen.findByText(/Supabase configured; authenticated storage access verified by a signed-in state request/i)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("http://localhost:8000/api/v1/state", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer signed-in-user-token" }),
    }));
  });
});
