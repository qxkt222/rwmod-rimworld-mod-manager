/**
 * Backups panel — empty state first, then a seeded backup zip exercises the
 * grouped list rendering and the version-list toggle.
 *
 * The fake zip is written into the temp-home backup dir; `list_backups()`
 * only inspects the filename, so no real mod data is involved.
 */
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { expect, gotoPanel, test } from "./helpers";

const BACKUP_NAME = "123456789__CoolMod__20260101_120000.zip";

test.describe("backups panel", () => {
  test("renders the empty state before any backup exists", async ({ page, backend }) => {
    await gotoPanel(page, backend, "backups");

    await expect(page.getByTestId("list-backups")).toContainText(
      "暂无备份。更新 Mod 时会自动创建备份。",
    );
  });

  test("lists backup groups and toggles the version list", async ({ page, backend }) => {
    const backupDir = path.join(backend.home, "backups");
    mkdirSync(backupDir, { recursive: true });
    writeFileSync(path.join(backupDir, BACKUP_NAME), "fake zip placeholder", "utf8");

    try {
      await gotoPanel(page, backend, "backups");

      const group = page.getByTestId("item-backup-123456789");
      await expect(group).toHaveCount(1);
      await expect(group.getByTestId("status-backup-folder")).toHaveText("CoolMod");
      await expect(group).toContainText("ID: 123456789");
      await expect(group).toContainText("1 个版本");
      await expect(page.getByTestId("status-backup-dir")).toContainText("备份目录:");

      // Versions panel starts hidden; the "1版" button toggles it.
      const versions = page.getByTestId("list-backup-versions-123456789");
      await expect(versions).toBeHidden();
      await group.getByRole("button", { name: /1版/ }).click();
      await expect(versions).toBeVisible();
      await expect(versions).toContainText("2026");
    } finally {
      rmSync(path.join(backupDir, BACKUP_NAME), { force: true });
    }
  });

  test("refresh button reloads the list", async ({ page, backend }) => {
    await gotoPanel(page, backend, "backups");
    await page.getByTestId("action-backup-refresh").click();
    await expect(page.getByTestId("list-backups")).toContainText(
      "暂无备份。更新 Mod 时会自动创建备份。",
    );
  });
});
