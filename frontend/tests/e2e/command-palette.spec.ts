/**
 * Ctrl+K command palette — filter, arrow-key selection, Enter execution and
 * Escape dismissal. Entirely client-side, so no API mocks are needed.
 */
import { entryURL, expect, gotoPanel, test } from "./helpers";

const PALETTE_ROWS = '[data-testid^="item-command-"]';

test.describe("command palette (Ctrl+K)", () => {
  test("opens with Ctrl+K, filters by label and closes on Escape", async ({ page, backend }) => {
    await page.goto(entryURL(backend));

    await page.keyboard.press("Control+k");
    const palette = page.getByTestId("dialog-command-palette");
    await expect(palette).toBeVisible();

    // Unfiltered list contains the full command set.
    const rows = page.locator(PALETTE_ROWS);
    await expect(rows.first()).toBeVisible();
    const total = await rows.count();
    expect(total).toBeGreaterThan(5);

    await page.getByTestId("field-command-input").fill("下载");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("下载 Mod");
    await expect(rows.nth(1)).toContainText("下载合集");
    await expect(rows.nth(2)).toContainText("下载队列");

    await page.keyboard.press("Escape");
    await expect(palette).toBeHidden();
  });

  test("arrow keys move the selection and Enter runs the command", async ({ page, backend }) => {
    await page.goto(entryURL(backend));
    await page.keyboard.press("Control+k");
    await page.getByTestId("field-command-input").fill("下载");

    const rows = page.locator(PALETTE_ROWS);
    await expect(rows.nth(0)).toHaveAttribute("aria-selected", "true");

    await page.keyboard.press("ArrowDown");
    await expect(rows.nth(1)).toHaveAttribute("aria-selected", "true");
    await expect(rows.nth(0)).toHaveAttribute("aria-selected", "false");

    await page.keyboard.press("ArrowUp");
    await expect(rows.nth(0)).toHaveAttribute("aria-selected", "true");

    // Execute "下载合集" (second row) → navigates to #collection.
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/#collection$/);
    await expect(page.locator("#panel-collection")).toHaveClass(/active/);
    await expect(page.getByTestId("dialog-command-palette")).toBeHidden();
  });

  test("toolbar ⚡ button opens the same palette", async ({ page, backend }) => {
    await gotoPanel(page, backend, "mods");
    await page.getByTestId("action-command-palette").click();
    await expect(page.getByTestId("dialog-command-palette")).toBeVisible();
  });
});
