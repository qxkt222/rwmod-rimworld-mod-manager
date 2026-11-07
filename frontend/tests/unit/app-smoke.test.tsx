/**
 * React scaffold smoke test (wave 0b).
 *
 * Proves the whole JSX chain is wired: Vitest picks up `.tsx`, the react-jsx
 * transform runs, @testing-library/react mounts into happy-dom, and
 * jest-dom matchers are registered. Panel behaviour stays covered by E2E.
 */
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { describe, expect, it } from "vitest";

import { App } from "../../src/app/App";

describe("App shell", () => {
  it("renders the title, version and status line", () => {
    render(<App version="0.7.0" status="已连接" />);

    expect(screen.getByTestId("app-shell")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /rwmod/i })).toBeInTheDocument();
    expect(screen.getByText("v0.7.0")).toBeInTheDocument();
    expect(screen.getByTestId("status-app")).toHaveTextContent("已连接");
  });
});
