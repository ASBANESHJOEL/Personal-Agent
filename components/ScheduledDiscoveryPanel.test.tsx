import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ScheduledDiscoveryPanel from "./ScheduledDiscoveryPanel";
import { getDiscoverySchedule, updateDiscoverySchedule, type DiscoverySchedule } from "../lib/api-client";

vi.mock("../lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api-client")>()),
  getDiscoverySchedule: vi.fn(),
  updateDiscoverySchedule: vi.fn(),
}));

const disabled: DiscoverySchedule = {
  configured: true, worker_deployed: true, enabled: false, schedule_hour_utc: 3,
  timezone: "UTC", next_scheduled_at: null, recent_runs: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getDiscoverySchedule).mockResolvedValue(disabled);
  vi.mocked(updateDiscoverySchedule).mockResolvedValue({ ...disabled, enabled: true, next_scheduled_at: "2026-10-21T03:00:00Z" });
});

describe("scheduled discovery settings", () => {
  it("loads disabled by default and changes the setting only after the API confirms it", async () => {
    let confirm!: (value: DiscoverySchedule) => void;
    vi.mocked(updateDiscoverySchedule).mockImplementation(() => new Promise((resolve) => { confirm = resolve; }));
    render(<ScheduledDiscoveryPanel accessToken="verified-user-token"/>);

    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(getDiscoverySchedule).toHaveBeenCalledWith("verified-user-token");
    fireEvent.click(screen.getByRole("button", { name: "Enable daily discovery" }));
    expect(updateDiscoverySchedule).toHaveBeenCalledWith("verified-user-token", true);
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(screen.getByText("Disabled")).toBeInTheDocument();

    confirm({ ...disabled, enabled: true, next_scheduled_at: "2026-10-21T03:00:00Z" });
    expect(await screen.findByRole("button", { name: "Disable daily discovery" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/Next:/)).toBeInTheDocument();
  });

  it("keeps the previous state on a failed toggle and permits retry", async () => {
    vi.mocked(updateDiscoverySchedule).mockRejectedValueOnce(new Error("Supabase unavailable"));
    render(<ScheduledDiscoveryPanel accessToken="verified-user-token"/>);
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable daily discovery" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Supabase unavailable");
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable daily discovery" })).toBeEnabled();
  });

  it("labels the cron worker as not deployed and presents persisted run failures", async () => {
    vi.mocked(getDiscoverySchedule).mockResolvedValue({
      ...disabled,
      worker_deployed: false,
      recent_runs: [{
        id: "run-1", status: "partially_failed", started_at: "2026-10-20T03:00:00Z", finished_at: "2026-10-20T03:01:00Z",
        source_results: [{ source: "github", status: "ok", count: 2 }, { source: "huggingface:model", status: "timeout", count: 0 }],
        new_discoveries: 2, updated_discoveries: 0, error_summary: "One provider failed.", attempt_count: 1,
      }],
    });
    render(<ScheduledDiscoveryPanel accessToken="verified-user-token"/>);
    expect(await screen.findByText(/not marked as deployed/)).toBeInTheDocument();
    expect(screen.getByText("partially failed · 1 source(s) succeeded, 1 failed")).toBeInTheDocument();
    expect(screen.getByText("One provider failed.")).toBeInTheDocument();
    await waitFor(() => expect(getDiscoverySchedule).toHaveBeenCalledTimes(1));
  });
});
