/**
 * History panel — empty history, zeroed stats, auto-update status, and the
 * clear action.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("history panel", () => {
  test("renders the empty state and zeroed statistics", async ({ page, backend }) => {
    await gotoPanel(page, backend, "history");

    await expect(page.getByTestId("list-history")).toContainText("暂无下载记录");
    await expect(page.getByTestId("status-history-stats")).toContainText("总计 0");
    await expect(page.getByTestId("status-history-stats")).toContainText("成功 0");
    await expect(page.getByTestId("status-history-stats")).toContainText("失败 0");
    await expect(page.getByTestId("status-auto-update")).toHaveText("就绪");
  });

  test("clear history keeps the empty state and shows a success toast", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "history");
    await page.getByTestId("action-history-clear").click();

    await expectToast(page, "历史记录已清空");
    await expect(page.getByTestId("list-history")).toContainText("暂无下载记录");
  });

  test("refresh re-reads the history from the backend", async ({ page, backend }) => {
    await gotoPanel(page, backend, "history");
    await page.getByTestId("action-history-refresh").click();
    await expect(page.getByTestId("status-history-stats")).toContainText("总计 0");
  });
});
