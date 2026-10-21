import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Dashboard from "./Dashboard";
import { AuthProvider } from "../lib/auth-context";

function renderDashboard() {
  return render(<AuthProvider><Dashboard /></AuthProvider>);
}

describe("dashboard shell", () => {
  it("shows demo disclosure and navigates between dashboard sections", () => {
    renderDashboard();
    expect(screen.getByText(/Overview sample cards are illustrative/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Integrations/ }));
    expect(screen.getByText("Integrations", { selector: "span" })).toBeInTheDocument();
  });

  it("filters sample discoveries and renders an empty state", () => {
    renderDashboard();
    fireEvent.change(screen.getByLabelText("Search opportunities"), { target: { value: "nothing-matches" } });
    expect(screen.getByText(/No sample opportunities match/)).toBeInTheDocument();
  });
});
