/**
 * Import panel — file-input flows render success/error toasts.
 *
 * The upload endpoints trigger real downloads on the backend, so they are
 * fulfilled with route mocks; the panel, DOM and toast pipeline are real.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

test.describe("import panel", () => {
  test("importing a .txt list reports per-file results (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/import/file", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ total: 2, results: [{ id: "111", ok: true }, { id: "222", ok: false }] }),
      }),
    );

    await gotoPanel(page, backend, "import");
    await page.getByTestId("field-import-file").setInputFiles({
      name: "mods.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("111\n222\n", "utf8"),
    });

    await expectToast(page, "导入完成: 1/2 成功");
  });

  test("importing ModsConfig.xml reports package/missing counts (backend mocked)", async ({
    page,
    backend,
  }) => {
    await page.route("**/api/import/sort", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          total_packages: 12,
          missing: 3,
          unknown: ["Unknown.Package"],
          downloaded: 3,
          results: [],
        }),
      }),
    );

    await gotoPanel(page, backend, "import");
    await page.getByTestId("field-import-sort").setInputFiles({
      name: "ModsConfig.xml",
      mimeType: "application/xml",
      buffer: Buffer.from("<ModsConfigData />", "utf8"),
    });

    await expectToast(page, /ModsConfig: 12 个包, 3 缺失, 3 已下载, 1 无法解析/);
  });

  test("backend failure surfaces as an error toast", async ({ page, backend }) => {
    await page.route("**/api/import/file", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: "InternalError", detail: "boom" }),
      }),
    );

    await gotoPanel(page, backend, "import");
    await page.getByTestId("field-import-file").setInputFiles({
      name: "mods.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("111\n", "utf8"),
    });

    await expectToast(page, "导入失败: boom");
  });
});
