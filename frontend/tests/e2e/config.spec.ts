/**
 * Config panel — read the seeded config, save it, and run the full
 * instance lifecycle (create → activate → switch back → delete).
 *
 * All paths live in the throwaway HOME; nothing touches a real RimWorld
 * install. Instance switching is refused while downloads are pending, which
 * the queue spec guarantees to clean up.
 */
import path from "node:path";
import { clickDialogButton, expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("config panel", () => {
  test("reads the seeded configuration and displays instance status", async ({ page, backend }) => {
    await gotoPanel(page, backend, "config");

    await expect(page.getByTestId("field-steamcmd-path")).toHaveValue(/steamcmd\.exe$/);
    await expect(page.getByTestId("field-mods-dir")).toHaveValue(/rwmod-e2e-.*[\\/]mods$/);
    await expect(page.getByTestId("field-rimworld-dir")).toHaveValue(/rwmod-e2e-.*[\\/]rimworld$/);
    await expect(page.getByTestId("status-config")).toContainText("SteamCMD:");
    await expect(page.getByTestId("status-config")).toContainText("Mods 目录:");
    await expect(page.getByTestId("list-instances")).toContainText("默认实例");
    await expect(page.getByTestId("list-instances")).toContainText("当前实例");
  });

  test("saves the configuration", async ({ page, backend }) => {
    await gotoPanel(page, backend, "config");
    await page.getByTestId("action-save-config").click();
    await expectToast(page, "配置已保存");
    await expect(page.getByTestId("status-config")).toContainText("SteamCMD:");
  });

  test("rejects a relative mods directory with a backend validation toast", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "config");
    await page.getByTestId("field-mods-dir").fill("relative/Mods");
    await page.getByTestId("action-save-config").click();
    await expectToast(page, /保存失败: mods_dir 必须是绝对路径/);
  });

  test("creates, activates, switches back and deletes a second instance", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "config");

    // ── create ──────────────────────────────────────────────────
    await page.getByTestId("field-new-instance-name").fill("E2E Second");
    await page.getByTestId("field-new-instance-mods").fill(path.join(backend.home, "mods2"));
    await page.getByTestId("field-new-instance-rimworld").fill(path.join(backend.home, "rimworld2"));
    await page.getByTestId("action-add-instance").click();
    await expectToast(page, "已创建实例 E2E Second");

    const second = page.locator('[data-testid^="item-instance-"]', { hasText: "E2E Second" });
    await expect(second).toBeVisible();

    // ── activate ────────────────────────────────────────────────
    await second.locator('[data-testid^="action-instance-activate-"]').click();
    await expectToast(page, "已切换到实例 E2E Second");
    await expect(second).toContainText("当前实例");
    await expect(page.getByTestId("field-mods-dir")).toHaveValue(/mods2$/);

    // ── switch back to the seeded default ───────────────────────
    const fallback = page.locator('[data-testid^="item-instance-"]', { hasText: "默认实例" });
    await fallback.locator('[data-testid^="action-instance-activate-"]').click();
    await expectToast(page, "已切换到实例 默认实例");
    await expect(fallback).toContainText("当前实例");
    await expect(page.getByTestId("field-mods-dir")).toHaveValue(/[\\/]mods$/);

    // ── delete the second instance ───────────────────────────────
    // React uses the in-app `dialog-instance-delete` ConfirmDialog.
    await second.locator('[data-testid^="action-instance-delete-"]').click();
    await clickDialogButton(page, "instance-delete", "删除");
    await expectToast(page, "已删除实例 E2E Second");
    await expect(page.getByTestId("list-instances")).not.toContainText("E2E Second");
  });
});
