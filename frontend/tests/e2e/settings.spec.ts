/**
 * Mod settings panel (Config/Mod_*.xml) — list, open the editor, enforce the
 * "game closed" acknowledgement, save, and verify a backup was created.
 * Writes stay inside the temp sandbox config dir.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

const NEW_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModSettings>
  <example>2</example>
  <touchedBy>e2e</touchedBy>
</ModSettings>
`;

test.describe("mod settings panel", () => {
  test("lists the seeded Mod_*.xml and shows its config directory", async ({ page, backend }) => {
    await gotoPanel(page, backend, "settings");

    const row = page.getByTestId("item-mod-setting-Mod_TestExample.xml");
    await expect(row).toBeVisible();
    await expect(page.getByTestId("status-settings")).toContainText("配置目录:");
    await expect(page.getByTestId("list-mod-setting-backups")).toContainText("暂无备份");
  });

  test("refuses to save without the game-closed acknowledgement", async ({ page, backend }) => {
    await gotoPanel(page, backend, "settings");
    await page.getByTestId("action-mod-setting-edit-Mod_TestExample.xml").click();

    await expect(page.getByTestId("dialog-mod-settings-editor")).toBeVisible();
    await expect(page.getByTestId("status-mod-settings-editor-title")).toContainText(
      "Mod_TestExample.xml",
    );

    await page.getByTestId("action-mod-settings-save").click();
    await expectToast(page, "请先勾选「我已关闭 RimWorld」");
  });

  test("opens the editor, saves XML and creates a backup", async ({ page, backend }) => {
    await gotoPanel(page, backend, "settings");
    await page.getByTestId("action-mod-setting-edit-Mod_TestExample.xml").click();

    const editor = page.getByTestId("field-mod-settings-xml");
    await expect(editor).toHaveValue(/<example>1<\/example>/);
    await editor.fill(NEW_XML);
    await page.getByTestId("field-mod-settings-ack").check();
    await page.getByTestId("action-mod-settings-save").click();

    await expectToast(page, /已保存（备份 /);
    await expect(page.getByTestId("list-mod-setting-backups")).toContainText(
      "Mod_TestExample.xml",
    );
    await expect(
      page.locator('[data-testid^="action-mod-backup-restore-"]').first(),
    ).toBeVisible();

    // Re-open the editor: the backend must now return the saved content.
    await page.getByTestId("action-mod-settings-close").click();
    await page.getByTestId("action-mod-setting-edit-Mod_TestExample.xml").click();
    await expect(page.getByTestId("field-mod-settings-xml")).toHaveValue(/touchedBy>e2e/);
  });

  test("backup-all creates backups for every Mod_*.xml", async ({ page, backend }) => {
    await gotoPanel(page, backend, "settings");
    await page.getByTestId("action-mod-settings-backup-all").click();
    await expectToast(page, /已备份 \d+ 个设置文件/);
  });
});
