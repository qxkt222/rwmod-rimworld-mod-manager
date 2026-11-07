/**
 * Mods panel — empty install renders the empty list and the batch toolbar
 * refuses to act without a selection. All endpoints are the real backend
 * (empty mods dir → no Steam API work).
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("mods panel", () => {
  test("renders the empty state for a fresh install", async ({ page, backend }) => {
    await gotoPanel(page, backend, "mods");

    await expect(page.getByTestId("list-mods")).toContainText("没有安装的 Mod");
    await expect(page.getByTestId("status-sidebar-mods-count")).toHaveText("0");
    await expect(page.getByTestId("action-mods-enable")).toBeVisible();
    await expect(page.getByTestId("action-mods-disable")).toBeVisible();
  });

  test("batch enable without a selection shows a validation toast", async ({ page, backend }) => {
    await gotoPanel(page, backend, "mods");
    await page.getByTestId("action-mods-enable").click();
    await expectToast(page, "请先勾选要操作的 Mod");
  });

  test("'仅看已启用' toggles without touching row data", async ({ page, backend }) => {
    await gotoPanel(page, backend, "mods");
    const checkbox = page.getByTestId("field-mods-only-active");
    await expect(checkbox).not.toBeChecked();
    await checkbox.check();
    await expect(checkbox).toBeChecked();
    // No rows exist in this fixture, so the filter is a no-op — assert the
    // empty state survives instead of silently blanking the panel.
    await expect(page.getByTestId("list-mods")).toContainText("没有安装的 Mod");
  });
});
