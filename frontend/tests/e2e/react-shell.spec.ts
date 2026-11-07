/**
 * React shell — the only frontend entry since the cutover, served from `/`.
 *
 * Covers the shared shell + dashboard pilot. The full 15-panel flows live in
 * the per-panel specs and also run against this entry (see helpers.ts /
 * tests/README.md).
 */
import { expect, test } from "./helpers";

const PANELS = [
  "dashboard",
  "download",
  "collection",
  "import",
  "mods",
  "search",
  "queue",
  "rimsort",
  "profiles",
  "history",
  "backups",
  "saves",
  "tags",
  "config",
  "settings",
] as const;

test.describe("react shell", () => {
  test("sidebar exposes all 15 panels and navigation updates hash + DOM", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/`);

    await expect(page.getByTestId("nav-sidebar")).toBeVisible();
    for (const panel of PANELS) {
      await expect(page.getByTestId(`nav-${panel}`)).toBeVisible();
    }

    await page.getByTestId("nav-mods").click();
    await expect(page).toHaveURL(/#mods$/);
    await expect(page.getByTestId("panel-mods")).toHaveAttribute("data-active", "true");
    await expect(page.getByTestId("nav-mods")).toHaveAttribute("aria-current", "page");
    await expect(page.locator(".panel.active")).toHaveCount(1);
  });

  test("dashboard stats and empty activity render from the real backend", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#dashboard`);

    await expect(page.locator("#db-mods-count")).toHaveText("0");
    await expect(page.locator("#db-updates-count")).toHaveText("0");
    await expect(page.locator("#db-disk-usage")).toHaveText("0 MB");
    await expect(page.locator("#db-activity")).toContainText("暂无活动记录");
    await expect(page.getByTestId("status-dashboard-mods")).toHaveText("0");
    await expect(page.locator("#queue-count")).toHaveText("0");
  });

  test("dashboard quick action navigates to the search panel", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}/#dashboard`);

    await page.getByTestId("action-quick-search").click();
    await expect(page).toHaveURL(/#search$/);
    await expect(page.getByTestId("panel-search")).toHaveAttribute("data-active", "true");
  });

  test("legacy #updates alias normalises to #queue", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}/#updates`);

    await expect(page).toHaveURL(/#queue$/);
    await expect(page.getByTestId("panel-queue")).toHaveAttribute("data-active", "true");
  });

  test("Ctrl+K opens the command palette, filters and Escape closes it", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#dashboard`);

    await page.keyboard.press("Control+k");
    await expect(page.locator("#cmd-palette")).toHaveClass(/open/);

    const rows = page.locator("#cmd-results .cmd-result");
    await expect(rows.first()).toBeVisible();
    expect(await rows.count()).toBeGreaterThan(5);

    await page.locator("#cmd-input").fill("下载");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("下载 Mod");
    await expect(rows.nth(1)).toContainText("下载合集");
    await expect(rows.nth(2)).toContainText("下载队列");

    await page.keyboard.press("Escape");
    await expect(page.locator("#cmd-palette")).not.toHaveClass(/open/);
  });
});
