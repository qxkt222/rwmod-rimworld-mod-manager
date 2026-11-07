/**
 * Cross-panel refresh bus — revision bumps drive `useRefreshOn`.
 */
import { act, cleanup, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { bump, getRevision, resetBus, useRefreshOn } from "../../src/store/bus";

function Probe() {
  const [count, setCount] = useState(0);
  useRefreshOn("mods", () => setCount((value) => value + 1));
  return <span data-testid="count">{count}</span>;
}

beforeEach(() => {
  resetBus();
});

afterEach(() => {
  cleanup();
});

describe("refresh bus", () => {
  it("increments only the bumped topic", () => {
    expect(getRevision("mods")).toBe(0);
    bump("mods");
    expect(getRevision("mods")).toBe(1);
    expect(getRevision("history")).toBe(0);
  });

  it("refreshes subscribers of the bumped topic only", () => {
    render(<Probe />);
    expect(screen.getByTestId("count")).toHaveTextContent("0");

    act(() => bump("mods"));
    expect(screen.getByTestId("count")).toHaveTextContent("1");

    act(() => bump("history"));
    expect(screen.getByTestId("count")).toHaveTextContent("1");

    act(() => bump("mods"));
    expect(screen.getByTestId("count")).toHaveTextContent("2");
  });
});
