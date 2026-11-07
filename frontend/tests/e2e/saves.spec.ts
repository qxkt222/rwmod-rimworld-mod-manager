/**
 * Saves panel — empty scan state and uploading a .rws file for analysis.
 * The upload path parses XML only; it never downloads mods.
 */
import { expect, expectToast, gotoPanel, test } from "./helpers";

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

test.describe("saves panel", () => {
  test("renders the empty scan state", async ({ page, backend }) => {
    await gotoPanel(page, backend, "saves");

    await expect(page.getByTestId("list-saves")).toContainText("No save files found");
    await expect(page.getByTestId("action-scan-saves")).toBeVisible();
  });

  test("uploads a .rws file and reports the parsed mod count", async ({ page, backend }) => {
    await gotoPanel(page, backend, "saves");
    await page.getByTestId("field-saves-upload").setInputFiles({
      name: "sample.rws",
      mimeType: "application/xml",
      buffer: Buffer.from(SAVE_XML, "utf8"),
    });

    await expectToast(page, "Found 2 mods in sample.rws");
  });

  test("scan button re-runs the scan", async ({ page, backend }) => {
    await gotoPanel(page, backend, "saves");
    await page.getByTestId("action-scan-saves").click();
    await expect(page.getByTestId("list-saves")).toContainText("No save files found");
  });
});
