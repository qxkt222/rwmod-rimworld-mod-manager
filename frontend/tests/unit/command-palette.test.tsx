/**
 * Command palette — filter, arrow-key selection, Enter execution and Escape
 * (mirrors the three legacy `command-palette.spec.ts` behaviours at unit level).
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CommandPalette } from "../../src/components/CommandPalette";
import { startRouter, stopRouter } from "../../src/router/hashRouter";
import { resetUI, useUIStore } from "../../src/store/ui";

beforeEach(() => {
  resetUI();
  window.history.replaceState({}, "", "/");
  startRouter();
  render(<CommandPalette />);
  act(() => useUIStore.getState().setCommandPalette(true));
});

afterEach(() => {
  cleanup();
  stopRouter();
  useUIStore.getState().setCommandPalette(false);
});

function input(): HTMLInputElement {
  return screen.getByPlaceholderText("输入命令...") as HTMLInputElement;
}

describe("CommandPalette", () => {
  it("opens with the full command set and filters by label", () => {
    expect(screen.getByTestId("dialog-command-palette")).toHaveClass("open");
    expect(screen.getAllByRole("option").length).toBeGreaterThan(5);

    fireEvent.change(input(), { target: { value: "下载" } });

    const rows = screen.getAllByRole("option");
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent("下载 Mod");
    expect(rows[1]).toHaveTextContent("下载合集");
    expect(rows[2]).toHaveTextContent("下载队列");
  });

  it("moves the selection with arrow keys and runs the command on Enter", () => {
    fireEvent.change(input(), { target: { value: "下载" } });
    expect(screen.getAllByRole("option")[0]).toHaveClass("selected");

    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(screen.getAllByRole("option")[1]).toHaveClass("selected");
    expect(screen.getAllByRole("option")[0]).not.toHaveClass("selected");

    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(screen.getAllByRole("option")[0]).toHaveClass("selected");

    // Second row is 下载合集 → #collection.
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "Enter" });

    expect(window.location.hash).toBe("#collection");
    expect(useUIStore.getState().commandPaletteOpen).toBe(false);
  });

  it("closes on Escape without navigating", () => {
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(useUIStore.getState().commandPaletteOpen).toBe(false);
    expect(window.location.hash).toBe("#dashboard");
  });
});
