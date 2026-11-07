/**
 * Dashboard pilot — stats, empty activity and populated activity rendering.
 * Network is stubbed at the HTTP boundary with MSW (no component internals).
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import { DashboardPanel } from "../../src/panels/DashboardPanel";
import { server } from "./setup";

afterEach(() => {
  cleanup();
  server.resetHandlers();
});

describe("DashboardPanel", () => {
  it("renders zero stats and the empty activity state", async () => {
    server.use(
      http.get("*/api/dashboard", () =>
        HttpResponse.json({
          mods_count: 0,
          updates_pending: 0,
          disk_usage_mb: 0,
          recent_activity: [],
        }),
      ),
    );

    render(<DashboardPanel />);

    await waitFor(() =>
      expect(screen.getByTestId("status-dashboard-mods")).toHaveTextContent("0"),
    );
    expect(screen.getByTestId("status-dashboard-updates")).toHaveTextContent("0");
    expect(screen.getByTestId("status-dashboard-disk")).toHaveTextContent("0 MB");
    expect(screen.getByTestId("list-activity")).toHaveTextContent("暂无活动记录");
  });

  it("renders stats, disk formatting and recent activity rows", async () => {
    server.use(
      http.get("*/api/dashboard", () =>
        HttpResponse.json({
          mods_count: 42,
          updates_pending: 3,
          disk_usage_mb: 2048,
          recent_activity: [
            {
              workshop_id: "123456",
              mod_name: "Test Mod",
              status: "success",
              created_at: "2026-01-02 03:04",
            },
            {
              workshop_id: "999",
              mod_name: "",
              status: "failed",
              created_at: "2026-01-01 00:00",
            },
          ],
        }),
      ),
    );

    render(<DashboardPanel />);

    await waitFor(() =>
      expect(screen.getByTestId("status-dashboard-mods")).toHaveTextContent("42"),
    );
    expect(screen.getByTestId("status-dashboard-updates")).toHaveTextContent("3");
    expect(screen.getByTestId("status-dashboard-disk")).toHaveTextContent("2.0 GB");
    expect(screen.getByTestId("item-activity-123456")).toHaveTextContent("Test Mod");
    // Falls back to the workshop id when the name is empty.
    expect(screen.getByTestId("item-activity-999")).toHaveTextContent("999");
  });
});
