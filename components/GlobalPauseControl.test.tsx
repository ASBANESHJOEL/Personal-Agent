import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getAgentState, setAgentPaused } from "../lib/api-client";
import GlobalPauseControl from "./GlobalPauseControl";

vi.mock("../lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api-client")>();
  return { ...actual, getAgentState: vi.fn(), setAgentPaused: vi.fn() };
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

beforeEach(() => vi.clearAllMocks());

describe("authenticated global pause control", () => {
  it("shows loading until the API confirms the initial state", async () => {
    const pending = deferred<{ paused: boolean }>();
    vi.mocked(getAgentState).mockReturnValueOnce(pending.promise);
    render(<GlobalPauseControl accessToken="session-token"/>);
    expect(screen.getByText("Loading server pause state…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    await act(async () => pending.resolve({ paused: true }));
    expect(await screen.findByText("Global pause is on")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume" })).toBeEnabled();
  });

  it("sends authenticated resume and pause requests and updates only from responses", async () => {
    vi.mocked(getAgentState).mockResolvedValueOnce({ paused: true });
    vi.mocked(setAgentPaused).mockResolvedValueOnce({ paused: false }).mockResolvedValueOnce({ paused: true });
    render(<GlobalPauseControl accessToken="session-token"/>);
    fireEvent.click(await screen.findByRole("button", { name: "Resume" }));
    expect(await screen.findByText("Global pause is off")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(await screen.findByText("Global pause is on")).toBeInTheDocument();
    expect(setAgentPaused).toHaveBeenNthCalledWith(1, "session-token", false);
    expect(setAgentPaused).toHaveBeenNthCalledWith(2, "session-token", true);
  });

  it("prevents duplicate pause writes while a request is pending", async () => {
    const pending = deferred<{ paused: boolean }>();
    vi.mocked(getAgentState).mockResolvedValueOnce({ paused: false });
    vi.mocked(setAgentPaused).mockReturnValueOnce(pending.promise);
    render(<GlobalPauseControl accessToken="session-token"/>);
    const pauseButton = await screen.findByRole("button", { name: "Pause" });
    fireEvent.click(pauseButton);
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Saving…" }));
    expect(setAgentPaused).toHaveBeenCalledTimes(1);
    await act(async () => pending.resolve({ paused: true }));
    expect(await screen.findByText("Global pause is on")).toBeInTheDocument();
  });

  it.each([{ initial: true, label: "Resume", target: false }, { initial: false, label: "Pause", target: true }])(
    "does not show success when $label fails",
    async ({ initial, label, target }) => {
      vi.mocked(getAgentState).mockResolvedValueOnce({ paused: initial });
      vi.mocked(setAgentPaused).mockRejectedValueOnce(new ApiError("Connection lost.", "network"));
      render(<GlobalPauseControl accessToken="session-token"/>);
      fireEvent.click(await screen.findByRole("button", { name: label }));
      expect(await screen.findByText("Pause state could not be confirmed")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent("Connection lost.");
      expect(setAgentPaused).toHaveBeenCalledWith("session-token", target);
      expect(screen.queryByText(`Global pause is ${target ? "on" : "off"}`)).not.toBeInTheDocument();
    },
  );

  it.each([
    { status: 401, message: /session is missing, expired, or invalid/i },
    { status: 403, message: /denied access to this user's state/i },
    { status: 503, message: /storage unavailable/i },
  ])("shows a distinct pause-state error for HTTP $status", async ({ status, message }) => {
    vi.mocked(getAgentState).mockRejectedValueOnce(new ApiError("Storage unavailable.", "http", status));
    render(<GlobalPauseControl accessToken="session-token"/>);
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText("Global pause is off")).not.toBeInTheDocument();
  });

  it("keeps pause state unknown when the authenticated read times out", async () => {
    vi.mocked(getAgentState).mockRejectedValueOnce(new ApiError("The backend request timed out.", "timeout"));
    render(<GlobalPauseControl accessToken="session-token"/>);
    expect(await screen.findByRole("alert")).toHaveTextContent("The backend request timed out.");
    expect(screen.queryByText("Global pause is off")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  });

  it("keeps the control disabled until an authenticated token is available", () => {
    render(<GlobalPauseControl accessToken={null}/>);
    expect(screen.getByText("Sign in to load server pause state")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
    expect(getAgentState).not.toHaveBeenCalled();
  });
});
