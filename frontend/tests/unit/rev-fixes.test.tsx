/**
 * Wave-4 review fixes (REV-004 / REV-008 / REV-009 / REV-016).
 *
 * Network is stubbed at the HTTP boundary with MSW; assertions only cover
 * user-visible behaviour (toasts, badges) and the public store actions.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModEntry } from "../../src/api/types";
import { bootstrap, stopBootstrap } from "../../src/app/bootstrap";
import { ToastViewport } from "../../src/components/ToastViewport";
import { Sidebar } from "../../src/layout/Sidebar";
import { DashboardPanel } from "../../src/panels/DashboardPanel";
import { DownloadPanel } from "../../src/panels/DownloadPanel";
import { useConfigStore } from "../../src/store/config";
import { useModsStore } from "../../src/store/mods";
import { useQueueStore } from "../../src/store/queue";
import { useTransferStore } from "../../src/store/transfer";
import { resetUI } from "../../src/store/ui";
import { useUpdatesStore } from "../../src/store/updates";
import { server } from "./setup";

// Bootstrap must not open a real socket inside happy-dom.
vi.mock("../../src/api/ws", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../src/api/ws")>();
  return { ...actual, startWsBridge: vi.fn(), stopWsBridge: vi.fn() };
});

function modEntry(folder: string): ModEntry {
  return {
    folder,
    name: folder,
    package_id: `pkg.${folder}`,
    workshop_id: "1",
    active: true,
  };
}

function configBody(activeInstance: string) {
  return {
    active_instance: activeInstance,
    instances: [],
    steamcmd_path: "",
    steamcmd_exists: false,
    mods_dir: "/mods",
    mods_dir_exists: true,
    rimworld_dir: "",
    backup_dir: "",
    has_steam_api_key: false,
  };
}

beforeEach(() => {
  resetUI();
  stopBootstrap();
  useTransferStore.getState().clear("download");
  useQueueStore.setState({ items: [], paused: false, status: "idle", error: null });
  useModsStore.setState({ items: [], status: "idle", error: null, selectedIds: [] });
  useUpdatesStore.setState({ autoUpdateRunning: false, error: null, checkStatus: "idle" });
  useConfigStore.setState({ steamOnline: null });
});

afterEach(() => {
  cleanup();
  stopBootstrap();
  server.resetHandlers();
});

describe("REV-004 auto-update conflict", () => {
  it("reports a 409 as an info toast, never as a failure", async () => {
    server.use(
      http.get("*/api/dashboard", () =>
        HttpResponse.json({
          mods_count: 0,
          updates_pending: 0,
          disk_usage_mb: 0,
          recent_activity: [],
        }),
      ),
      http.post("*/api/auto-update/run", () =>
        HttpResponse.json({ error: "ConflictError", detail: "已在运行中" }, { status: 409 }),
      ),
    );

    render(
      <>
        <DashboardPanel />
        <ToastViewport />
      </>,
    );
    await waitFor(() =>
      expect(screen.getByTestId("status-dashboard-mods")).toHaveTextContent("0"),
    );

    fireEvent.click(screen.getByTestId("action-auto-update"));

    await waitFor(() =>
      expect(screen.getByTestId("status-toast")).toHaveTextContent("更新检查已在运行中"),
    );
    expect(screen.getByTestId("status-toast")).toHaveAttribute("data-type", "info");
    expect(screen.queryByText(/检查失败/)).not.toBeInTheDocument();
  });

  it("keeps the shared runner flag on when another client owns the check", async () => {
    server.use(
      http.post("*/api/auto-update/run", () =>
        HttpResponse.json({ error: "ConflictError", detail: "已在运行中" }, { status: 409 }),
      ),
    );

    await expect(useUpdatesStore.getState().runAutoUpdate()).rejects.toMatchObject({
      status: 409,
    });
    expect(useUpdatesStore.getState().autoUpdateRunning).toBe(true);
  });
});

describe("REV-008 download batch result", () => {
  it("shows an error toast instead of 下载完成 when the stream fails", async () => {
    server.use(
      http.get("*/api/download/stream*", () =>
        HttpResponse.text(`data: ${JSON.stringify({ event: "fail", msg: "boom" })}\n\n`, {
          headers: { "content-type": "text/event-stream" },
        }),
      ),
      http.get("*/api/queue", () => HttpResponse.json({ items: [], paused: false })),
      http.get("*/api/mods", () => HttpResponse.json([])),
    );

    render(
      <>
        <DownloadPanel />
        <ToastViewport />
      </>,
    );

    fireEvent.change(screen.getByTestId("field-mod-input"), { target: { value: "123" } });
    fireEvent.click(screen.getByTestId("action-download"));

    await waitFor(
      () => expect(screen.getByTestId("status-toast")).toHaveTextContent("下载失败"),
      { timeout: 5_000 },
    );
    expect(screen.getByTestId("status-toast")).toHaveAttribute("data-type", "error");
    expect(screen.queryByText("下载完成")).not.toBeInTheDocument();
  });

  it("keeps the success toast when every id finishes", async () => {
    server.use(
      http.get("*/api/download/stream*", () =>
        HttpResponse.text(`data: ${JSON.stringify({ event: "done", id: "123" })}\n\n`, {
          headers: { "content-type": "text/event-stream" },
        }),
      ),
      http.get("*/api/queue", () => HttpResponse.json({ items: [], paused: false })),
      http.get("*/api/mods", () => HttpResponse.json([])),
    );

    render(
      <>
        <DownloadPanel />
        <ToastViewport />
      </>,
    );

    fireEvent.change(screen.getByTestId("field-mod-input"), { target: { value: "123" } });
    fireEvent.click(screen.getByTestId("action-download"));

    await waitFor(
      () => expect(screen.getByTestId("status-toast")).toHaveTextContent("下载完成"),
      { timeout: 5_000 },
    );
    expect(screen.getByTestId("status-toast")).toHaveAttribute("data-type", "success");
    expect(screen.queryByText("下载失败")).not.toBeInTheDocument();
  });
});

describe("REV-009 sidebar mod badge", () => {
  it("loads the mod list during bootstrap", async () => {
    server.use(
      http.get("*/api/config", () => HttpResponse.json(configBody("default"))),
      http.get("*/api/mods", () => HttpResponse.json([modEntry("A"), modEntry("B")])),
      http.get("*/api/queue", () => HttpResponse.json({ items: [], paused: false })),
      http.get("*/api/auto-update/status", () => HttpResponse.json({ running: false })),
      http.get("*/api/status", () => HttpResponse.json({ online: false })),
    );

    await bootstrap();

    expect(useModsStore.getState().items).toHaveLength(2);
    // Let the fire-and-forget online probe finish before MSW is torn down.
    await waitFor(() => expect(useConfigStore.getState().steamOnline).toBe(false));
  });

  it("refreshes the badge after switching to another instance", async () => {
    useConfigStore.setState({ activeInstanceId: "alpha", config: null });
    useModsStore.setState({ items: [modEntry("A"), modEntry("B")], status: "ready", error: null });

    server.use(
      http.post("*/api/config/instances/:id/activate", ({ params }) =>
        HttpResponse.json({ ok: true, active_instance: params.id as string }),
      ),
      http.get("*/api/config", () => HttpResponse.json(configBody("beta"))),
      http.get("*/api/mods", () => HttpResponse.json([modEntry("C"), modEntry("D"), modEntry("E")])),
    );

    render(<Sidebar />);
    expect(document.getElementById("mod-count")).toHaveTextContent("2");

    await act(async () => {
      await useConfigStore.getState().activateInstance("beta");
    });

    await waitFor(() => expect(document.getElementById("mod-count")).toHaveTextContent("3"));
  });
});

describe("REV-016 queue delete scope", () => {
  it("carries the active instance id on DELETE /api/queue/{id}", async () => {
    useConfigStore.setState({ activeInstanceId: "alpha" });

    let deleteUrl = "";
    server.use(
      http.delete("*/api/queue/:id", ({ request }) => {
        deleteUrl = request.url;
        return HttpResponse.json({ ok: true });
      }),
      http.get("*/api/queue", () => HttpResponse.json({ items: [], paused: false })),
    );

    await useQueueStore.getState().remove("999");

    expect(deleteUrl).toContain("instance_id=alpha");
  });

  it("carries the active instance id on POST /api/queue/retry", async () => {
    useConfigStore.setState({ activeInstanceId: "alpha" });

    let retryUrl = "";
    let retryBody: unknown = null;
    server.use(
      http.post("*/api/queue/retry", async ({ request }) => {
        retryUrl = request.url;
        retryBody = await request.json();
        return HttpResponse.json({ accepted: 1 });
      }),
      http.get("*/api/queue", () => HttpResponse.json({ items: [], paused: false })),
    );

    await useQueueStore.getState().retry(["999"]);

    expect(retryUrl).toContain("instance_id=alpha");
    expect(retryBody).toEqual({ ids: ["999"] });
  });
});
