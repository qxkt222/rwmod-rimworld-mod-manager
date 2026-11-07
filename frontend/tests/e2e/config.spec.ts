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

    await expect(page.locator("#cfg-steamcmd")).toHaveValue(/steamcmd\.exe$/);
    await expect(page.locator("#cfg-mods")).toHaveValue(/rwmod-e2e-.*[\\/]mods$/);
    await expect(page.locator("#cfg-rimworld")).toHaveValue(/rwmod-e2e-.*[\\/]rimworld$/);
    await expect(page.locator("#config-display")).toContainText("SteamCMD:");
    await expect(page.locator("#config-display")).toContainText("Mods 目录:");
    await expect(page.locator("#instance-list")).toContainText("默认实例");
    await expect(page.locator("#instance-list")).toContainText("当前实例");
  });

  test("saves the configuration", async ({ page, backend }) => {
    await gotoPanel(page, backend, "config");
    await page.locator("#btn-save-config").click();
    await expectToast(page, "配置已保存");
    await expect(page.locator("#config-display")).toContainText("SteamCMD:");
  });

  test("rejects a relative mods directory with a backend validation toast", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "config");
    await page.locator("#cfg-mods").fill("relative/Mods");
    await page.locator("#btn-save-config").click();
    await expectToast(page, /保存失败: mods_dir 必须是绝对路径/);
  });

  test("creates, activates, switches back and deletes a second instance", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "config");

    // ── create ──────────────────────────────────────────────────
    await page.locator("#new-instance-name").fill("E2E Second");
    await page.locator("#new-instance-mods").fill(path.join(backend.home, "mods2"));
    await page.locator("#new-instance-rimworld").fill(path.join(backend.home, "rimworld2"));
    await page.locator("#btn-add-instance").click();
    await expectToast(page, "已创建实例 E2E Second");

    const second = page.locator("#instance-list > div", { hasText: "E2E Second" });
    await expect(second).toBeVisible();

    // ── activate ────────────────────────────────────────────────
    await second.locator('button[data-action="activate"]').click();
    await expectToast(page, "已切换到实例 E2E Second");
    await expect(second).toContainText("当前实例");
    await expect(page.locator("#cfg-mods")).toHaveValue(/mods2$/);

    // ── switch back to the seeded default ───────────────────────
    const fallback = page.locator("#instance-list > div", { hasText: "默认实例" });
    await fallback.locator('button[data-action="activate"]').click();
    await expectToast(page, "已切换到实例 默认实例");
    await expect(fallback).toContainText("当前实例");
    await expect(page.locator("#cfg-mods")).toHaveValue(/[\\/]mods$/);

    // ── delete the second instance ───────────────────────────────
    // React uses the in-app `dialog-instance-delete` ConfirmDialog.
    await second.locator('button[data-action="delete"]').click();
    await clickDialogButton(page, "instance-delete", "删除");
    await expectToast(page, "已删除实例 E2E Second");
    await expect(page.locator("#instance-list")).not.toContainText("E2E Second");
  });
});
