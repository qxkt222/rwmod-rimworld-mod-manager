/**
 * Collection panel — preview counts render from the real UI.
 *
 * Downloading a collection would run SteamCMD, so only the read-only preview
 * endpoint is mocked. No real collection is ever downloaded.
 */
import { expect, gotoPanel, test } from "./helpers";

test.describe("collection panel", () => {
  test("preview renders collection totals (backend mocked)", async ({ page, backend }) => {
    await page.route("**/api/collection/preview/*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          collection_id: "3721899704",
          total: 3,
          installed_count: 1,
          new_count: 2,
          failed_count: 1,
        }),
      }),
    );

    await gotoPanel(page, backend, "collection");
    await page.getByTestId("field-collection-input").fill("3721899704");
    await page.getByTestId("action-collection-preview").click();

    const log = page.getByTestId("list-collection-log");
    await expect(log).toContainText("合集 3721899704: 3 个 Mod");
    await expect(log).toContainText("已安装 1");
    await expect(log).toContainText("新发现 2");
    await expect(log).toContainText("之前失败 1");
    await expect(log).toContainText('点击"下载合集"开始下载');
  });

  test("empty input does not fire a request", async ({ page, backend }) => {
    let requested = false;
    await page.route("**/api/collection/preview/*", (route) => {
      requested = true;
      return route.abort();
    });

    await gotoPanel(page, backend, "collection");
    await page.getByTestId("action-collection-preview").click();
    await page.waitForTimeout(300);
    expect(requested).toBe(false);
  });
});
