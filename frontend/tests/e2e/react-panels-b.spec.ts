/**
 * React entry (wave 2, FE-3) — panels B: queue / search / profiles / rimsort /
 * download / collection / import.
 *
 * Mirrors the per-panel specs but drives `/#<panel>`.
 * Endpoints that would hit the Steam Workshop / launch SteamCMD are mocked,
 * exactly as in the other specs (never a real download).
 */
import { apiPost, expect, expectToast, test } from "./helpers";

const REACT = "/";

test.describe("react panels b — queue", () => {
  test.afterEach(async ({ request, backend }) => {
    const state = await request.get(`${backend.baseURL}/api/queue`);
    if (!state.ok()) return;
    const body = (await state.json()) as { items?: { id: string }[] };
    for (const item of body.items ?? []) {
      await request.delete(`${backend.baseURL}/api/queue/${encodeURIComponent(item.id)}`);
    }
  });

  test("renders a pending item through the React panel and removes it", async ({
    page,
    request,
    backend,
  }) => {
    await apiPost(request, backend, "/api/queue/add", { ids: ["999999"], force: false });

    await page.goto(`${backend.baseURL}${REACT}#queue`);

    const row = page.getByTestId("item-queue-999999");
    await expect(row).toBeVisible();
    await expect(row).toContainText("pending");
    await expect(page.getByTestId("action-queue-pause")).toBeVisible();

    await page.getByTestId("action-queue-remove-999999").click();
    await expect(page.getByTestId("list-queue")).toContainText(
      "队列为空，去「搜索」或「下载」添加 Mod",
    );
  });

  test("update check renders the changelog and expands it (backend mocked)", async ({
    page,
    backend,
  }) => {
    const changelog = "第一条更新说明。".repeat(40);
    await page.route("**/api/mods/check-updates", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          updates: [
            {
              workshop_id: "777",
              name: "Old Mod",
              folder: "OldMod",
              remote_title: "Old Mod",
              time_updated: 1_700_000_000,
              file_description: changelog,
            },
          ],
        }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#queue`);
    await page.getByTestId("action-check-updates").click();

    const results = page.getByTestId("list-update-results");
    await expect(results).toContainText("Old Mod");
    await expect(results).toContainText("Workshop 777");
    await expect(page.getByTestId("action-update-all")).toBeVisible();

    await page.getByTestId("action-changelog-toggle-777").click();
    await expect(page.getByTestId("status-changelog-full-777")).toBeVisible();
    await expect(page.getByTestId("status-changelog-full-777")).toContainText(
      "第一条更新说明",
    );
    await expect(page.getByTestId("action-changelog-toggle-777")).toHaveText("📖 收起");
  });
});

test.describe("react panels b — search", () => {
  test("renders results and adds a hit to the queue (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/search*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          results: [
            {
              id: "2009463077",
              title: "Vanilla Expanded Framework",
              author: "Oskar Potocki",
              description: "Framework for vanilla expanded mods",
              preview_url: "",
              rating: "5",
              subscribers: "123456",
              installed: true,
            },
          ],
        }),
      }),
    );
    await page.route("**/api/queue/add", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ added: 1, items: [{ id: "2009463077", status: "pending" }] }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#search`);
    await page.getByTestId("field-search-query").fill("vanilla expanded");
    await page.getByTestId("action-search").click();

    await expect(page.getByTestId("status-search-hit-title-2009463077")).toHaveText(
      "Vanilla Expanded Framework",
    );
    await expect(page.getByTestId("item-search-2009463077")).toContainText("✓ 已安装");
    await expect(page.getByTestId("item-search-2009463077")).toContainText("by Oskar Potocki");

    await page.getByTestId("action-add-queue-2009463077").click();
    await expectToast(page, "已加入队列: 2009463077");
  });

  test("search failure renders the inline error (backend mocked)", async ({ page, backend }) => {
    await page.route("**/api/search*", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "ServiceUnavailable", detail: "steam down" }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#search`);
    await page.getByTestId("field-search-query").fill("x");
    await page.getByTestId("action-search").click();

    await expect(page.getByTestId("list-search-results")).toContainText(
      "搜索失败: steam down",
    );
  });
});

test.describe("react panels b — profiles", () => {
  test("saves a profile through the in-app dialog and deletes it", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}${REACT}#profiles`);

    await expect(page.getByTestId("status-profile-path")).toContainText("ModsConfig.xml");
    await expect(page.getByTestId("list-profiles")).toContainText("暂无存档");
    await expect(page.getByTestId("status-undo")).toContainText("暂无撤销快照");

    await page.getByTestId("action-profile-save").click();
    await expect(page.getByTestId("dialog-profile-save")).toBeVisible();
    await page.getByTestId("field-profile-name").fill("React Profile B");
    await page.getByTestId("action-profile-save-confirm").click();

    await expectToast(page, "已保存 'React Profile B'");
    const item = page.getByTestId("item-profile-React Profile B");
    await expect(item).toBeVisible();
    await expect(item).toContainText("1 个 Mod");

    await page.getByTestId("action-profile-delete-React Profile B").click();
    await expect(page.getByTestId("dialog-profile-confirm")).toBeVisible();
    await page.getByTestId("dialog-profile-confirm").getByRole("button", { name: "删除" }).click();

    await expectToast(page, "已删除");
    await expect(page.getByTestId("list-profiles")).toContainText("暂无存档");
  });
});

const COMPARE_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModsConfigData>
  <version>1.5.4104 rev435</version>
  <activeMods>
    <li>Ludeon.RimWorld</li>
    <li>SomeAuthor.TestMod</li>
    <li>AnotherAuthor.MissingMod</li>
  </activeMods>
</ModsConfigData>
`;

test.describe("react panels b — rimsort", () => {
  test("generates ModsConfig.xml from the installed mods", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}${REACT}#rimsort`);
    await page.getByTestId("action-rimsort-generate").click();

    const output = page.getByTestId("status-rimsort-output");
    await expect(output).toBeVisible();
    await expect(output).toContainText("ModsConfigData");
    await expect(output).toContainText("activeMods");
  });

  test("uploaded ModsConfig.xml comparison and load-order analysis render", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}${REACT}#rimsort`);
    await page.getByTestId("field-rimsort-file").setInputFiles({
      name: "ModsConfig.xml",
      mimeType: "application/xml",
      buffer: Buffer.from(COMPARE_XML, "utf8"),
    });

    const result = page.getByTestId("status-rimsort-compare");
    await expect(result).toContainText("对比结果");
    await expect(result).toContainText("配置中有 3 个 Mod");
    await expect(result).toContainText("✅ 已安装 0");
    await expect(result).toContainText("❌ 缺失 2");
    await expect(result).toContainText("缺失的 Mod (2)");
    await expect(result).toContainText("SomeAuthor.TestMod");
    await expect(result).toContainText("无法解析 Workshop ID");

    await page.getByTestId("action-rimsort-order").click();
    const order = page.getByTestId("status-rimsort-order");
    await expect(order).toContainText("排序分析");
    await expect(order).toContainText("1 个 Mod");
    await expect(order).toContainText("未检测到 Harmony");
    await expect(order).toContainText("1 警告");
  });
});

test.describe("react panels b — download", () => {
  test("streams SSE log lines through the transfer store (backend mocked)", async ({
    page,
    backend,
  }) => {
    const sse = [
      `data: ${JSON.stringify({ event: "start", id: "123456" })}`,
      "",
      `data: ${JSON.stringify({ event: "info", msg: "正在解析 Workshop ID" })}`,
      "",
      `data: ${JSON.stringify({ event: "ok", msg: "下载成功" })}`,
      "",
      `data: ${JSON.stringify({ event: "done", id: "123456" })}`,
      "",
      "",
    ].join("\n");

    await page.route("**/api/download/stream*", (route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: sse,
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#download`);
    await page.getByTestId("field-mod-input").fill("123456");
    await page.getByTestId("action-download").click();

    await expect(page.getByTestId("list-log")).toContainText("▶ 开始下载 123456");
    await expect(page.getByTestId("list-log")).toContainText("正在解析 Workshop ID");
    await expect(page.getByTestId("list-log")).toContainText("下载成功");
    await expect(page.getByTestId("list-log")).toContainText("✅ 完成 123456");

    await page.getByTestId("action-clear-log").click();
    await expect(page.getByTestId("list-log")).toHaveText("");
  });

  test("dependency preview renders installed and missing deps (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/mods/dependencies", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          deps: {
            "123456": [
              { id: "111", name: "Harmony", installed: true },
              { id: "222", name: "HugsLib", installed: false },
            ],
          },
        }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#download`);
    await page.getByTestId("field-mod-input").fill("123456");

    await expect(page.getByTestId("status-dependency-preview")).toContainText("Harmony", {
      timeout: 5_000,
    });
    await expect(page.getByTestId("status-dependency-preview")).toContainText("HugsLib");
    await expect(page.getByTestId("status-dependency-preview")).toContainText("未安装");
    await expect(page.getByTestId("status-dependency-preview")).toContainText("1 个依赖缺失");
  });
});

test.describe("react panels b — collection", () => {
  test("preview renders collection totals (backend mocked)", async ({ page, backend }) => {
    await page.route("**/api/collection/preview/*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          collection_id: "3721899704",
          total: 3,
          installed_count: 1,
          new_count: 2,
          failed_count: 1,
        }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#collection`);
    await page.getByTestId("field-collection-input").fill("3721899704");
    await page.getByTestId("action-collection-preview").click();

    const log = page.getByTestId("list-collection-log");
    await expect(log).toContainText("合集 3721899704: 3 个 Mod");
    await expect(log).toContainText("已安装 1");
    await expect(log).toContainText("新发现 2");
    await expect(log).toContainText("之前失败 1");
    await expect(log).toContainText('点击"下载合集"开始下载');
  });
});

test.describe("react panels b — import", () => {
  test("importing a .txt list reports per-file results (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/import/file", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          total: 2,
          results: [
            { id: "111", ok: true },
            { id: "222", ok: false },
          ],
        }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#import`);
    await page.getByTestId("field-import-file").setInputFiles({
      name: "mods.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("111\n222\n", "utf8"),
    });

    await expectToast(page, "导入完成: 1/2 成功");
  });

  test("importing ModsConfig.xml reports package/missing counts (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/import/sort", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          total_packages: 12,
          missing: 3,
          unknown: ["Unknown.Package"],
          downloaded: 3,
          results: [],
        }),
      }),
    );

    await page.goto(`${backend.baseURL}${REACT}#import`);
    await page.getByTestId("field-import-sort").setInputFiles({
      name: "ModsConfig.xml",
      mimeType: "application/xml",
      buffer: Buffer.from("<ModsConfigData />", "utf8"),
    });

    await expectToast(page, /ModsConfig: 12 个包, 3 缺失, 3 已下载, 1 无法解析/);
  });
});
