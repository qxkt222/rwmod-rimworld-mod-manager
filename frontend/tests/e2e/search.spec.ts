/**
 * Search panel — Workshop search results and "add to queue" action.
 *
 * Both endpoints touch the Steam Workshop (network), so they are mocked. The
 * panel markup, escaping and event wiring are the real built frontend.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("search panel", () => {
  test("renders results and adds a hit to the queue (backend mocked)", async ({ page, backend }) => {
    await page.route("**/api/search*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          results: [
            {
              id: "2009463077",
              title: "Vanilla Expanded Framework",
              author: "Oskar Potocki",
              description: "Framework for vanilla expanded mods",
              preview_url: "",
              rating: "5",
              subscribers: "123456",
              installed: true,
            },
          ],
        }),
      }),
    );
    await page.route("**/api/queue/add", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ added: 1, items: [{ id: "2009463077", status: "pending" }] }),
      }),
    );

    await gotoPanel(page, backend, "search");
    await page.locator("#search-input").fill("vanilla expanded");
    await page.locator("#btn-search").click();

    await expect(page.locator(".search-hit-title")).toHaveText("Vanilla Expanded Framework");
    await expect(page.locator(".search-hit")).toContainText("✓ 已安装");
    await expect(page.locator(".search-hit")).toContainText("by Oskar Potocki");

    await page.locator('.search-hit button[data-id="2009463077"]').click();
    await expectToast(page, "已加入队列: 2009463077");
  });

  test("empty result set renders the no-results state (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/search*", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ results: [] }),
      }),
    );

    await gotoPanel(page, backend, "search");
    await page.locator("#search-input").fill("nothing matches");
    await page.locator("#btn-search").click();

    await expect(page.locator("#search-results")).toContainText("无结果");
  });

  test("search failure renders an error message", async ({ page, backend }) => {
    await page.route("**/api/search*", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "ServiceUnavailable", detail: "steam down" }),
      }),
    );

    await gotoPanel(page, backend, "search");
    await page.locator("#search-input").fill("x");
    await page.locator("#btn-search").click();

    await expect(page.locator("#search-results")).toContainText("搜索失败: steam down");
  });
});
