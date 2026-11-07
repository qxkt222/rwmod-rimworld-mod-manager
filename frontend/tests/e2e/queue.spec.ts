/**
 * Queue panel — real queue rows from the real backend, plus the update-check
 * card that lives inside this panel.
 *
 * A pending item is seeded via the real /api/queue/add and removed through
 * the UI's ✕ button, so the shared backend stays clean for other specs.
 * /api/mods/check-updates touches the Steam API → mocked. No queue is ever
 * started, therefore SteamCMD never runs.
 */
import { apiGet, apiPost, expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("queue panel", () => {
  test.afterEach(async ({ request, backend }) => {
    // Defensive cleanup: never leave pending rows behind for other specs
    // (config instance switching refuses to run with pending downloads).
    const state = await apiGet<{ items: { id: string }[] }>(request, backend, "/api/queue");
    for (const item of state.items) {
      await request.delete(`${backend.baseURL}/api/queue/${encodeURIComponent(item.id)}`);
    }
  });

  test("renders a pending queue item and removes it through the UI", async ({
    page,
    request,
    backend,
  }) => {
    await apiPost(request, backend, "/api/queue/add", { ids: ["999999"], force: false });

    await gotoPanel(page, backend, "queue");
    const row = page.locator(".queue-item", { hasText: "999999" });
    await expect(row).toBeVisible();
    await expect(row).toContainText("pending");

    await row.locator('[data-remove="999999"]').click();
    await expect(page.locator("#queue-list")).toContainText(
      "队列为空，去「搜索」或「下载」添加 Mod",
    );
  });

  test("empty queue renders the instruction empty state", async ({ page, backend }) => {
    await gotoPanel(page, backend, "queue");
    await expect(page.locator("#queue-list")).toContainText(
      "队列为空，去「搜索」或「下载」添加 Mod",
    );
    await expect(page.locator("#btn-queue-start")).toBeVisible();
    await expect(page.locator("#btn-queue-pause")).toBeVisible();
  });

  test("update check renders changelog rows and expands the full text (backend mocked)", async ({
    page,
    backend,
  }) => {
    const changelog = "第一条更新说明。".repeat(40); // > 200 chars → expand button
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

    await gotoPanel(page, backend, "queue");
    await page.locator("#btn-check-updates").click();

    const results = page.locator("#update-results");
    await expect(results).toContainText("Old Mod");
    await expect(results).toContainText("Workshop 777");
    await expect(page.locator("#btn-update-all")).toBeVisible();

    await results.locator(".changelog-toggle").click();
    await expect(results.locator(".changelog-full")).toBeVisible();
    await expect(results.locator(".changelog-full")).toContainText("第一条更新说明");
    await expect(results.locator(".changelog-toggle")).toHaveText("📖 收起");
  });

  test("no updates renders the all-up-to-date state (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/mods/check-updates", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ updates: [] }),
      }),
    );

    await gotoPanel(page, backend, "queue");
    await page.locator("#btn-check-updates").click();
    await expect(page.locator("#update-results")).toContainText("✅ 所有 Mod 均为最新版本");
  });

  test("clear-completed button only clears finished items", async ({ page, request, backend }) => {
    await apiPost(request, backend, "/api/queue/add", { ids: ["888888"], force: false });
    await gotoPanel(page, backend, "queue");
    await expect(page.locator(".queue-item", { hasText: "888888" })).toBeVisible();

    await page.locator("#btn-queue-clear").click();
    // Pending rows must survive a "clear completed" pass.
    await expect(page.locator(".queue-item", { hasText: "888888" })).toBeVisible();
  });
});
