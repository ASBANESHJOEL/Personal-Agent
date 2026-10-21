import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DiscoveryPanel from "./DiscoveryPanel";
import { getDiscovery, runDiscovery, updateDiscoveryStatus, type DiscoveryItem } from "../lib/api-client";

vi.mock("../lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/api-client")>()),
  getDiscovery: vi.fn(), runDiscovery: vi.fn(), updateDiscoveryStatus: vi.fn(),
}));

const result: DiscoveryItem = {
  id: "item-1", platform: "github", kind: "repository", source_identity: "vision-lab/robotics",
  source_url: "https://github.com/vision-lab/robotics", title: "vision-lab/robotics", description: "Robotics and computer vision",
  metadata: { topics: ["robotics", "computer-vision"], stars: 21, updated_at: "2026-10-19T00:00:00Z" },
  ranking_score: 82, ranking_reasons: ["Topic match: robotics", "Popularity metadata is available"], status: "proposed",
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getDiscovery).mockResolvedValue([]);
  vi.mocked(runDiscovery).mockResolvedValue({ items: [result], sources: [{ source: "github", status: "ok", count: 1 }, { source: "huggingface:model", status: "rate_limited", count: 0, detail: "Provider rate limit reached; try again later." }], query: "robotics" });
  vi.mocked(updateDiscoveryStatus).mockResolvedValue({ ...result, status: "saved" });
});

describe("live discovery panel", () => {
  it("loads persisted records, runs a filtered query and saves only after server confirmation", async () => {
    vi.mocked(getDiscovery).mockResolvedValue([result]);
    render(<DiscoveryPanel accessToken="verified-token"/>);
    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search discovery results"), { target: { value: "robotics" } });
    fireEvent.click(screen.getByRole("button", { name: "Discover now" }));
    await waitFor(() => expect(runDiscovery).toHaveBeenCalledWith("verified-token", expect.objectContaining({ query: "robotics", platforms: ["github", "huggingface"], page: 1 })));
    expect(await screen.findByText(/huggingface:model: Provider rate limit/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(updateDiscoveryStatus).toHaveBeenCalledWith("verified-token", "item-1", "saved"));
    fireEvent.change(screen.getByLabelText("Filter discovery status"), { target: { value: "saved" } });
    expect(await screen.findByRole("button", { name: "Saved" })).toBeDisabled();
  });

  it("shows a truthful sign-in state and does not send an unauthenticated discovery request", () => {
    render(<DiscoveryPanel accessToken={null}/>);
    expect(screen.getByText(/Sign in with Supabase Auth/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Discover now" })).toBeDisabled();
    expect(getDiscovery).not.toHaveBeenCalled();
    expect(runDiscovery).not.toHaveBeenCalled();
  });

  it("surfaces retrieval failures without displaying stale records as current results", async () => {
    vi.mocked(getDiscovery).mockRejectedValue(new Error("backend unavailable"));
    render(<DiscoveryPanel accessToken="verified-token"/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("backend unavailable");
    expect(screen.getByText("No discoveries yet")).toBeInTheDocument();
  });

  it("does not claim a save succeeded when the backend rejects the status update", async () => {
    vi.mocked(runDiscovery).mockResolvedValue({ items: [result], sources: [{ source: "github", status: "ok", count: 1 }], query: "robotics" });
    vi.mocked(updateDiscoveryStatus).mockRejectedValue(new Error("storage unavailable"));
    render(<DiscoveryPanel accessToken="verified-token"/>);
    fireEvent.click(screen.getByRole("button", { name: "Discover now" }));
    fireEvent.click(await screen.findByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("storage unavailable");
    expect(screen.getByRole("button", { name: "Save" })).toBeEnabled();
  });

  it("dismisses only after server confirmation and keeps the item recoverable in the dismissed filter", async () => {
    vi.mocked(getDiscovery).mockResolvedValue([result]);
    let confirm!: (item: DiscoveryItem) => void;
    vi.mocked(updateDiscoveryStatus).mockImplementation(() => new Promise((resolve) => { confirm = resolve; }));
    render(<DiscoveryPanel accessToken="verified-token"/>);

    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(updateDiscoveryStatus).toHaveBeenCalledWith("verified-token", "item-1", "dismissed");
    expect(screen.getByText("vision-lab/robotics")).toBeInTheDocument();

    confirm({ ...result, status: "dismissed" });
    await waitFor(() => expect(screen.queryByText("vision-lab/robotics")).not.toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Filter discovery status"), { target: { value: "dismissed" } });
    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    expect(screen.getByText("dismissed")).toBeInTheDocument();
  });

  it("keeps failed dismissals visible and allows retry", async () => {
    vi.mocked(getDiscovery).mockResolvedValue([result]);
    vi.mocked(updateDiscoveryStatus).mockRejectedValueOnce(new Error("status update unavailable"));
    vi.mocked(updateDiscoveryStatus).mockResolvedValueOnce({ ...result, status: "dismissed" });
    render(<DiscoveryPanel accessToken="verified-token"/>);
    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("status update unavailable");
    expect(screen.getByText("vision-lab/robotics")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(updateDiscoveryStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText("vision-lab/robotics")).not.toBeInTheDocument());
  });

  it("preserves a dismissed record across refresh and a repeated discovery run", async () => {
    const dismissed = { ...result, status: "dismissed" as const };
    vi.mocked(getDiscovery).mockResolvedValue([dismissed]);
    vi.mocked(runDiscovery).mockResolvedValue({ items: [dismissed], sources: [{ source: "github", status: "ok", count: 1 }], query: "robotics" });
    const { unmount } = render(<DiscoveryPanel accessToken="verified-token"/>);
    expect(await screen.findByText("No results match these filters")).toBeInTheDocument();
    unmount();

    render(<DiscoveryPanel accessToken="verified-token"/>);
    await waitFor(() => expect(getDiscovery).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByLabelText("Filter discovery status"), { target: { value: "dismissed" } });
    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Discover now" }));
    await waitFor(() => expect(runDiscovery).toHaveBeenCalled());
    expect(await screen.findByText("vision-lab/robotics")).toBeInTheDocument();
    expect(screen.getByText("dismissed")).toBeInTheDocument();
    expect(screen.getAllByRole("article")).toHaveLength(1);
  });
});
