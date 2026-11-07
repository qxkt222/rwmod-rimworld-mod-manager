/**
 * Tags panel — the tag list, per-tag folder detail and create / rename /
 * delete management.
 */
import { apiPost, expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("tags panel", () => {
  test("renders the empty state before any tag exists", async ({ page, backend }) => {
    await gotoPanel(page, backend, "tags");
    await expect(page.getByTestId("list-tags")).toContainText("No tags yet");
  });

  test("lists a seeded tag and shows its mod folders", async ({ page, request, backend }) => {
    await apiPost(request, backend, "/api/tags/E2EModFolder", { tag: "e2e-combat" });
    try {
      await gotoPanel(page, backend, "tags");

      const row = page.getByTestId("item-tag-e2e-combat");
      await expect(row).toBeVisible();
      await expect(row).toContainText("1 mods");

      await row.click();
      await expect(page.getByTestId("status-tags-detail")).toContainText("#e2e-combat");
      await expect(page.getByTestId("status-tags-detail")).toContainText("E2EModFolder");
    } finally {
      await request.delete(`${backend.baseURL}/api/tags/E2EModFolder/e2e-combat`);
    }
  });

  test("creates, renames and deletes tags from the UI", async ({ page, request, backend }) => {
    const folder = "E2ECRUDModFolder";
    try {
      await gotoPanel(page, backend, "tags");

      // ── create ─────────────────────────────────────────────────
      await page.getByTestId("field-tag-folder").fill(folder);
      await page.getByTestId("field-tag-name").fill("e2e-original");
      await page.getByTestId("action-tag-add").click();
      await expectToast(page, "已添加标签 #e2e-original");

      const row = page.getByTestId("item-tag-e2e-original");
      await expect(row).toBeVisible();
      await expect(row).toContainText("1 mods");

      await row.click();
      await expect(page.getByTestId("status-tags-detail")).toContainText("#e2e-original");
      await expect(page.getByTestId("status-tags-detail")).toContainText(folder);

      // ── rename ─────────────────────────────────────────────────
      await page.getByTestId("field-tag-rename").fill("e2e-renamed");
      await page.getByTestId("action-tag-rename").click();
      await expectToast(page, /已重命名标签 #e2e-original/);
      await expect(page.getByTestId("item-tag-e2e-renamed")).toBeVisible();
      await expect(page.getByTestId("item-tag-e2e-original")).toHaveCount(0);
      await expect(page.getByTestId("status-tags-detail")).toContainText("#e2e-renamed");

      // ── delete (in-app confirm dialog) ────────────────────────
      await page.getByTestId("item-tag-e2e-renamed").click();
      await page.getByTestId("action-tag-delete").click();
      const dialog = page.getByTestId("dialog-tag-delete");
      await expect(dialog).toBeVisible();
      await dialog.getByRole("button", { name: "删除" }).click();
      await expectToast(page, "已删除标签 #e2e-renamed");
      await expect(page.getByTestId("item-tag-e2e-renamed")).toHaveCount(0);
    } finally {
      // Best-effort cleanup of either name so a mid-test failure cannot leak
      // tags into the shared worker backend.
      for (const tag of ["e2e-original", "e2e-renamed"]) {
        await request.delete(`${backend.baseURL}/api/tags/${folder}/${tag}`);
      }
    }
  });
});
