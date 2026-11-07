/**
 * React entry (wave 2, FE-2) — behaviour coverage for the seven A-group panels
 * on `/`: history, backups, saves, tags, config, settings and mods. Every case
 * drives the real backend (except the .rws upload, which only parses XML) and
 * asserts the canonical testids plus the legacy anchors the older suite
 * depends on.
 */
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { apiPost, expect, expectToast, test } from "./helpers";

const BACKUP_NAME = "123456789__CoolMod__20260101_120000.zip";

const SAVE_XML = `<?xml version="1.0" encoding="utf-8"?>
<savegame>
  <meta>
    <modIds>
      <li>Ludeon.RimWorld</li>
      <li>SomeAuthor.TestMod</li>
    </modIds>
  </meta>
</savegame>
`;

const NEW_SETTINGS_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModSettings>
  <example>2</example>
  <touchedBy>react-e2e</touchedBy>
</ModSettings>
`;

/**
 * Restore the shared temp home after a settings test.
 *
 * `settings.spec.ts` runs against the same worker-scoped
 * backend and assumes the seeded Mod_TestExample.xml plus an empty backup
 * list. Saving always creates a backup and rewrites the file, so the cleanup
 * puts both back exactly as the fixture seeded them.
 */
function restoreSettingsFixture(home: string, original: Buffer): void {
  try {
    writeFileSync(path.join(home, "config", "Mod_TestExample.xml"), original);
  } catch {
    /* best effort — the temp home is reaped on teardown */
  }
  rmSync(path.join(home, ".rwmod", "mod_settings_backups"), {
    recursive: true,
    force: true,
  });
}

test.describe("react A panels", () => {
  // ── history ─────────────────────────────────────────────────────
  test("history renders empty rows, zero stats and the auto-update status", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#history`);

    await expect(page.getByTestId("panel-history")).toHaveAttribute("data-active", "true");
    await expect(page.locator("#history-list")).toContainText("暂无下载记录");
    await expect(page.locator("#history-stats")).toContainText("总计 0");
    await expect(page.locator("#history-stats")).toContainText("成功 0");
    await expect(page.locator("#history-stats")).toContainText("失败 0");
    await expect(page.locator("#auto-update-status")).toHaveText("就绪");
    await expect(page.getByTestId("list-history")).toBeVisible();
  });

  test("history clear shows the success toast and keeps the empty state", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#history`);
    await page.locator("#btn-clear-history").click();

    await expectToast(page, "历史记录已清空");
    await expect(page.locator("#history-list")).toContainText("暂无下载记录");
  });

  // ── backups ─────────────────────────────────────────────────────
  test("backups renders the empty state, then groups a seeded backup", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#backups`);
    await expect(page.locator("#backup-list")).toContainText(
      "暂无备份。更新 Mod 时会自动创建备份。",
    );

    const backupDir = path.join(backend.home, "backups");
    mkdirSync(backupDir, { recursive: true });
    writeFileSync(path.join(backupDir, BACKUP_NAME), "fake zip placeholder", "utf8");

    try {
      await page.locator("#btn-backup-refresh").click();

      const group = page.locator(".backup-group");
      await expect(group).toHaveCount(1);
      await expect(group.locator(".mod-name")).toHaveText("CoolMod");
      await expect(group).toContainText("ID: 123456789");
      await expect(group).toContainText("1 个版本");
      await expect(page.locator("#backup-group-label")).toContainText("备份目录:");

      const versions = page.locator("#versions-123456789");
      await expect(versions).toBeHidden();
      await group.getByRole("button", { name: /1版/ }).click();
      await expect(versions).toBeVisible();
      await expect(versions).toContainText("2026");
    } finally {
      rmSync(path.join(backupDir, BACKUP_NAME), { force: true });
    }
  });

  // ── saves ───────────────────────────────────────────────────────
  test("saves scans the empty Saves directory and analyses an uploaded .rws", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#saves`);
    await expect(page.locator("#saves-results")).toContainText("No save files found");
    await expect(page.locator("#btn-scan-saves")).toBeVisible();

    await page.locator("#saves-file-upload").setInputFiles({
      name: "sample.rws",
      mimeType: "application/xml",
      buffer: Buffer.from(SAVE_XML, "utf8"),
    });

    await expectToast(page, "Found 2 mods in sample.rws");
  });

  // ── tags ────────────────────────────────────────────────────────
  test("tags lists a seeded tag and shows its mod folders", async ({
    page,
    request,
    backend,
  }) => {
    await apiPost(request, backend, "/api/tags/E2EModFolder", { tag: "e2e-combat-a" });
    try {
      await page.goto(`${backend.baseURL}/#tags`);

      const row = page.locator(".tag-row", { hasText: "e2e-combat-a" });
      await expect(row).toBeVisible();
      await expect(row).toContainText("1 mods");

      await row.click();
      await expect(page.locator("#tags-detail")).toContainText("#e2e-combat-a");
      await expect(page.locator("#tags-detail")).toContainText("E2EModFolder");
    } finally {
      await request.delete(`${backend.baseURL}/api/tags/E2EModFolder/e2e-combat-a`);
    }
  });

  test("tags can be created, renamed and deleted from the UI", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}/#tags`);

    // create
    await page.locator("#tag-folder-input").fill("ReactTagFolder");
    await page.locator("#tag-name-input").fill("react-original");
    await page.locator("#btn-add-tag").click();
    await expectToast(page, "已添加标签 #react-original");

    const original = page.locator(".tag-row", { hasText: "react-original" });
    await expect(original).toBeVisible();
    await original.click();
    await expect(page.locator("#tags-detail")).toContainText("#react-original");

    // rename
    await page.locator("#tag-rename-input").fill("react-renamed");
    await page.locator("#btn-rename-tag").click();
    await expectToast(page, /已重命名标签 #react-original/);
    await expect(page.locator(".tag-row", { hasText: "react-renamed" })).toBeVisible();
    await expect(page.locator(".tag-row", { hasText: "react-original" })).toHaveCount(0);

    // delete (in-app confirm dialog, not native confirm)
    await page.locator(".tag-row", { hasText: "react-renamed" }).click();
    await page.locator("#btn-delete-tag").click();
    const dialog = page.getByTestId("dialog-tag-delete");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "删除" }).click();
    await expectToast(page, "已删除标签 #react-renamed");
    await expect(page.locator(".tag-row", { hasText: "react-renamed" })).toHaveCount(0);
  });

  // ── config ──────────────────────────────────────────────────────
  test("config reads the seeded configuration and saves it", async ({ page, backend }) => {
    await page.goto(`${backend.baseURL}/#config`);

    await expect(page.locator("#cfg-steamcmd")).toHaveValue(/steamcmd\.exe$/);
    await expect(page.locator("#cfg-mods")).toHaveValue(/[\\/]mods$/);
    await expect(page.locator("#cfg-rimworld")).toHaveValue(/[\\/]rimworld$/);
    await expect(page.locator("#config-display")).toContainText("SteamCMD:");
    await expect(page.locator("#config-display")).toContainText("Mods 目录:");
    await expect(page.locator("#instance-list")).toContainText("默认实例");
    await expect(page.locator("#instance-list")).toContainText("当前实例");

    await page.locator("#btn-save-config").click();
    await expectToast(page, "配置已保存");
  });

  test("config creates, activates, switches back and deletes an instance", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#config`);

    await page.locator("#new-instance-name").fill("React Second");
    await page.locator("#new-instance-mods").fill(path.join(backend.home, "mods2"));
    await page.locator("#new-instance-rimworld").fill(path.join(backend.home, "rimworld2"));
    await page.locator("#btn-add-instance").click();
    await expectToast(page, "已创建实例 React Second");

    const second = page.locator("#instance-list > div", { hasText: "React Second" });
    await expect(second).toBeVisible();

    await second.locator('button[data-action="activate"]').click();
    await expectToast(page, "已切换到实例 React Second");
    await expect(second).toContainText("当前实例");
    await expect(page.locator("#cfg-mods")).toHaveValue(/mods2$/);

    const fallback = page.locator("#instance-list > div", { hasText: "默认实例" });
    await fallback.locator('button[data-action="activate"]').click();
    await expectToast(page, "已切换到实例 默认实例");
    await expect(fallback).toContainText("当前实例");

    await page
      .locator("#instance-list > div", { hasText: "React Second" })
      .locator('button[data-action="delete"]')
      .click();
    const dialog = page.getByTestId("dialog-instance-delete");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: "删除" }).click();
    await expectToast(page, "已删除实例 React Second");
    await expect(page.locator("#instance-list")).not.toContainText("React Second");
  });

  // ── settings ────────────────────────────────────────────────────
  test("settings lists the seeded XML, enforces the ack and saves a backup", async ({
    page,
    backend,
  }) => {
    const settingsPath = path.join(backend.home, "config", "Mod_TestExample.xml");
    const original = readFileSync(settingsPath);

    try {
      await page.goto(`${backend.baseURL}/#settings`);

      const row = page.locator(".mod-row", { hasText: "Mod_TestExample.xml" });
      await expect(row).toBeVisible();
      await expect(page.locator("#mod-settings-status")).toContainText("配置目录:");

      await page.locator('.mod-setting-edit[data-file="Mod_TestExample.xml"]').click();
      await expect(page.locator("#mod-settings-editor")).toBeVisible();
      await expect(page.locator("#mod-settings-editor-title")).toContainText(
        "Mod_TestExample.xml",
      );
      await expect(page.locator("#mod-settings-xml")).toHaveValue(/<ModSettings>/);

      // Saving without the "game is closed" acknowledgement is refused.
      await page.locator("#btn-mod-settings-save").click();
      await expectToast(page, "请先勾选「我已关闭 RimWorld」");

      await page.locator("#mod-settings-xml").fill(NEW_SETTINGS_XML);
      await page.locator("#mod-settings-ack").check();
      await page.locator("#btn-mod-settings-save").click();
      await expectToast(page, /已保存（备份 /);
      await expect(page.locator("#mod-settings-backups")).toContainText("Mod_TestExample.xml");
    } finally {
      restoreSettingsFixture(backend.home, original);
    }
  });

  test("settings backup-all reports the backed-up file count", async ({ page, backend }) => {
    const settingsPath = path.join(backend.home, "config", "Mod_TestExample.xml");
    const original = readFileSync(settingsPath);
    try {
      await page.goto(`${backend.baseURL}/#settings`);
      await page.locator("#btn-mod-settings-backup-all").click();
      await expectToast(page, /已备份 \d+ 个设置文件/);
    } finally {
      restoreSettingsFixture(backend.home, original);
    }
  });

  // ── mods ────────────────────────────────────────────────────────
  test("mods renders the empty install and validates batch actions", async ({
    page,
    backend,
  }) => {
    await page.goto(`${backend.baseURL}/#mods`);

    await expect(page.locator("#mod-list")).toContainText("没有安装的 Mod");
    await expect(page.locator("#mod-count")).toHaveText("0");
    await expect(page.locator("#btn-mods-enable")).toBeVisible();
    await expect(page.locator("#btn-mods-disable")).toBeVisible();

    await page.locator("#btn-mods-enable").click();
    await expectToast(page, "请先勾选要操作的 Mod");

    const checkbox = page.locator("#mods-only-active");
    await expect(checkbox).not.toBeChecked();
    await checkbox.check();
    await expect(checkbox).toBeChecked();
    await expect(page.locator("#mod-list")).toContainText("没有安装的 Mod");
  });
});
