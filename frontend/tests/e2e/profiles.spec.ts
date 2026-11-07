/**
 * Profiles panel — save a profile from the seeded ModsConfig.xml, see it
 * listed, then delete it. All writes target the temp sandbox home.
 */
import { clickDialogButton, expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("profiles panel", () => {
  test("saves a profile from ModsConfig.xml and deletes it", async ({ page, backend }) => {
    await gotoPanel(page, backend, "profiles");

    await expect(page.locator("#profile-modsconfig-path")).toContainText("ModsConfig.xml");
    await expect(page.locator("#profile-list")).toContainText("暂无存档");
    await expect(page.locator("#undo-status")).toContainText("暂无撤销快照");

    // Save through the in-app `dialog-profile-save` NameDialog.
    await page.locator("#btn-profile-save").click();
    const saveDialog = page.getByTestId("dialog-profile-save");
    await expect(saveDialog).toBeVisible();
    await saveDialog.getByTestId("field-profile-name").fill("E2E Profile");
    await saveDialog.getByTestId("action-profile-save-confirm").click();

    await expectToast(page, "已保存 'E2E Profile'");
    const item = page.locator(".profile-item", { hasText: "E2E Profile" });
    await expect(item).toBeVisible();
    await expect(item).toContainText("1 个 Mod");

    // Delete through `dialog-profile-confirm`.
    await item.locator('[data-delete="E2E Profile"]').click();
    await clickDialogButton(page, "profile-confirm", "删除");
    await expectToast(page, "已删除");
    await expect(page.locator("#profile-list")).toContainText("暂无存档");
    await expect(page.locator(".profile-item", { hasText: "E2E Profile" })).toHaveCount(0);
  });

  test("cancelling the save prompt creates nothing", async ({ page, backend }) => {
    await gotoPanel(page, backend, "profiles");
    await expect(page.locator("#profile-list")).toContainText("暂无存档");

    // Cancel must be a real cancellation: open the in-app
    // `dialog-profile-save`, dismiss it, then assert nothing was created and
    // no success toast fired.
    await page.locator("#btn-profile-save").click();
    const dialog = page.getByTestId("dialog-profile-save");
    await expect(dialog).toBeVisible();
    await clickDialogButton(page, "profile-save", "取消");
    await expect(dialog).toBeHidden();
    await page.waitForTimeout(300);

    await expect(page.locator("#profile-list")).toContainText("暂无存档");
    await expect(page.locator(".profile-item")).toHaveCount(0);
    await expect(page.locator("#toast-container")).not.toContainText("已保存");
  });
});
