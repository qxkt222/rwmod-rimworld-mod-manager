/**
 * Dashboard panel — zero-data stats, empty activity list and a quick action.
 */
import { expect, gotoPanel, test } from "./helpers";

test.describe("dashboard panel", () => {
  test("renders zero stats and the empty activity state from the real backend", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "dashboard");

    await expect(page.getByTestId("status-dashboard-mods")).toHaveText("0");
    await expect(page.getByTestId("status-dashboard-updates")).toHaveText("0");
    await expect(page.getByTestId("status-dashboard-disk")).toHaveText("0 MB");
    await expect(page.getByTestId("list-activity")).toContainText("暂无活动记录");
    await expect(page.getByTestId("status-sidebar-queue-count")).toHaveText("0");
  });

  test("quick action navigates to the search panel", async ({ page, backend }) => {
    await gotoPanel(page, backend, "dashboard");
    await page.getByRole("button", { name: /搜索 Workshop/ }).click();
    await expect(page.locator("#panel-search")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#search$/);
  });
});
