/**
 * History panel — empty history, zeroed stats, auto-update status, and the
 * clear action.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("history panel", () => {
  test("renders the empty state and zeroed statistics", async ({ page, backend }) => {
    await gotoPanel(page, backend, "history");

    await expect(page.locator("#history-list")).toContainText("暂无下载记录");
    await expect(page.locator("#history-stats")).toContainText("总计 0");
    await expect(page.locator("#history-stats")).toContainText("成功 0");
    await expect(page.locator("#history-stats")).toContainText("失败 0");
    await expect(page.locator("#auto-update-status")).toHaveText("就绪");
  });

  test("clear history keeps the empty state and shows a success toast", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "history");
    await page.locator("#btn-clear-history").click();

    await expectToast(page, "历史记录已清空");
    await expect(page.locator("#history-list")).toContainText("暂无下载记录");
  });

  test("refresh re-reads the history from the backend", async ({ page, backend }) => {
    await gotoPanel(page, backend, "history");
    await page.locator("#btn-history-refresh").click();
    await expect(page.locator("#history-stats")).toContainText("总计 0");
  });
});
