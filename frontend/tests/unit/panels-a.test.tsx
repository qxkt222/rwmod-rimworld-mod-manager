/**
 * Component-layer tests for the FE-2 A-group panels.
 *
 * Network is stubbed at the HTTP boundary with MSW; assertions only cover
 * user-visible behaviour (empty states, stats, create flow, file list), not
 * component internals. Full flows stay in Playwright (`react-panels-a.spec.ts`).
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { HistoryPanel } from "../../src/panels/HistoryPanel";
import { SettingsPanel } from "../../src/panels/SettingsPanel";
import { TagsPanel } from "../../src/panels/TagsPanel";
import { useHistoryStore } from "../../src/store/history";
import { useSettingsStore } from "../../src/store/settings";
import { useTagsStore } from "../../src/store/tags";
import { server } from "./setup";

beforeEach(() => {
  useHistoryStore.setState({
    items: [],
    stats: { total: 0, success: 0, failed: 0 },
    status: "idle",
    error: null,
  });
  useTagsStore.setState({
    tags: [],
    detail: null,
    status: "idle",
    detailStatus: "idle",
    error: null,
    mutating: false,
  });
  useSettingsStore.setState({
    files: [],
    configDir: null,
    backups: [],
    status: "idle",
    error: null,
    editor: null,
    editorLoading: false,
    saving: false,
  });
});

afterEach(() => {
  cleanup();
  server.resetHandlers();
});

describe("HistoryPanel", () => {
  it("renders the empty list and zeroed statistics", async () => {
    server.use(
      http.get("*/api/history", () => HttpResponse.json({ items: [] })),
      http.get("*/api/history/stats", () =>
        HttpResponse.json({ total: 0, success: 0, failed: 0 }),
      ),
      http.get("*/api/auto-update/status", () => HttpResponse.json({ running: false })),
    );

    render(<HistoryPanel />);

    await waitFor(() =>
      expect(screen.getByTestId("status-history-empty")).toHaveTextContent("暂无下载记录"),
    );
    expect(screen.getByTestId("status-history-stats")).toHaveTextContent("总计 0");
    expect(screen.getByTestId("status-history-stats")).toHaveTextContent("成功 0");
    expect(screen.getByTestId("status-history-stats")).toHaveTextContent("失败 0");
    expect(screen.getByTestId("status-auto-update")).toHaveTextContent("就绪");
  });
});

describe("TagsPanel", () => {
  it("creates a tag from the management form and reloads the list", async () => {
    let tags: { tag: string; count: number }[] = [];
    server.use(
      http.get("*/api/tags", () => HttpResponse.json({ tags })),
      http.post("*/api/tags/:folder", async ({ request, params }) => {
        const body = (await request.json()) as { tag?: string };
        tags = [...tags, { tag: body.tag ?? "", count: 1 }];
        return HttpResponse.json({ ok: true, tag: body.tag, folder: params.folder });
      }),
    );

    render(<TagsPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("status-tags-empty")).toHaveTextContent("No tags yet"),
    );

    fireEvent.change(screen.getByTestId("field-tag-folder"), {
      target: { value: "NewFolder" },
    });
    fireEvent.change(screen.getByTestId("field-tag-name"), { target: { value: "combat" } });
    fireEvent.click(screen.getByTestId("action-tag-add"));

    await waitFor(() =>
      expect(screen.getByTestId("item-tag-combat")).toHaveTextContent("combat"),
    );
    expect(screen.getByTestId("item-tag-combat")).toHaveTextContent("1 mods");
  });
});

describe("SettingsPanel", () => {
  it("lists the Mod_*.xml files from the backend", async () => {
    server.use(
      http.get("*/api/mod-settings", () =>
        HttpResponse.json({
          config_dir: "C:/RimWorld/Config",
          files: [
            {
              filename: "Mod_TestExample.xml",
              size: 128,
              mtime: "2026-01-01T00:00:00Z",
              associated: true,
              workshop_id: "123",
              package_id: "test.mod",
              name: "Test Mod",
            },
          ],
        }),
      ),
      http.get("*/api/mod-settings/backups", () =>
        HttpResponse.json({ backup_dir: "C:/backups", backups: [] }),
      ),
    );

    render(<SettingsPanel />);

    await waitFor(() =>
      expect(screen.getByTestId("item-mod-setting-Mod_TestExample.xml")).toHaveTextContent(
        "Mod_TestExample.xml",
      ),
    );
    expect(screen.getByTestId("status-settings")).toHaveTextContent("配置目录:");
    expect(screen.getByTestId("list-mod-setting-backups")).toHaveTextContent("暂无备份");
  });
});
