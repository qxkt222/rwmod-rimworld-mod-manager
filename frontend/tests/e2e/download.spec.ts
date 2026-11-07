/**
 * Download panel.
 *
 * The real UI is served by the real backend; only the SSE download stream and
 * the dependency lookup are fulfilled with route mocks, because exercising
 * them for real would launch SteamCMD / hit the Steam Workshop. This is the
 * "能 mock 则 mock" path from the task book — never a real download.
 */
import { expect, gotoPanel, test } from "./helpers";

test.describe("download panel", () => {
  test("streams SSE log lines and the final done event (backend mocked)", async ({
    page,
    backend,
  }) => {
    const sse = [
      `data: ${JSON.stringify({ event: "start", id: "123456" })}`,
      "",
      `data: ${JSON.stringify({ event: "info", msg: "正在解析 Workshop ID" })}`,
      "",
      `data: ${JSON.stringify({
        event: "progress",
        id: "123456",
        percent: 42.5,
        downloaded: 1024,
        total: 4096,
      })}`,
      "",
      `data: ${JSON.stringify({ event: "ok", msg: "下载成功" })}`,
      "",
      `data: ${JSON.stringify({ event: "done", id: "123456" })}`,
      "",
      "",
    ].join("\n");

    await page.route("**/api/download/stream*", (route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
        body: sse,
      }),
    );

    await gotoPanel(page, backend, "download");
    await page.locator("#mod-input").fill("123456");
    await page.locator("#btn-download").click();

    // The leading "✓" is decorative (legacy prefixes ok lines, React logs the
    // raw SSE message), so the contract is the event text and its order,
    // including the terminal done line.
    await expect(page.locator("#log")).toContainText(
      /▶ 开始下载 123456[\s\S]*正在解析 Workshop ID[\s\S]*下载成功[\s\S]*✅ 完成 123456/,
    );
  });

  test("dependency preview renders installed and missing deps (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/mods/dependencies", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          deps: {
            "123456": [
              { id: "111", name: "Harmony", installed: true },
              { id: "222", name: "HugsLib", installed: false },
            ],
          },
        }),
      }),
    );

    await gotoPanel(page, backend, "download");
    await page.locator("#mod-input").fill("123456");

    // The panel debounces the lookup by 500 ms.
    await expect(page.locator("#dep-preview")).toContainText("Harmony", { timeout: 5_000 });
    await expect(page.locator("#dep-preview")).toContainText("HugsLib");
    await expect(page.locator("#dep-preview")).toContainText("未安装");
    await expect(page.locator("#dep-preview")).toContainText("1 个依赖缺失");
  });

  test("clear-log button empties the console", async ({ page, backend }) => {
    await page.route("**/api/download/stream*", (route) =>
      route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: `data: ${JSON.stringify({ event: "start", id: "42" })}\n\ndata: ${JSON.stringify({ event: "done", id: "42" })}\n\n`,
      }),
    );

    await gotoPanel(page, backend, "download");
    await page.locator("#mod-input").fill("42");
    await page.locator("#btn-download").click();
    await expect(page.locator("#log")).toContainText("▶ 开始下载 42");

    await page.locator("#btn-clear-log").click();
    await expect(page.locator("#log")).toHaveText("");
  });
});
