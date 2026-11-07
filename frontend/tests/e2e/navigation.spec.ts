/**
 * Navigation — every registered panel is reachable, the URL hash stays in
 * sync, and browser back/forward restore the right panel.
 *
 * Panel list mirrors frontend/src/panel-registry.ts PANEL_NAMES (15 entries).
 * `updates` is an alias of `queue`, not a panel of its own.
 */
import { entryURL, expect, gotoPanel, navControl, test } from "./helpers";

const PANELS = [
  "dashboard",
  "download",
  "collection",
  "import",
  "mods",
  "search",
  "queue",
  "rimsort",
  "profiles",
  "history",
  "backups",
  "saves",
  "tags",
  "config",
  "settings",
] as const;

test.describe("navigation", () => {
  test("all 15 panels can be switched and the URL hash follows", async ({ page, backend }) => {
    await page.goto(entryURL(backend));
    await expect(page.locator("#panel-dashboard")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#dashboard$/);

    for (const panel of PANELS) {
      await navControl(page, panel).click();
      await expect(page.locator(`#panel-${panel}`)).toHaveClass(/active/);
      await expect(navControl(page, panel)).toHaveClass(/active/);
      await expect(page).toHaveURL(new RegExp(`#${panel}$`));
    }
  });

  test("browser back/forward restores the previous panel", async ({ page, backend }) => {
    await page.goto(entryURL(backend, "dashboard"));

    await navControl(page, "download").click();
    await expect(page).toHaveURL(/#download$/);
    await navControl(page, "mods").click();
    await expect(page).toHaveURL(/#mods$/);

    await page.goBack();
    await expect(page.locator("#panel-download")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#download$/);

    await page.goForward();
    await expect(page.locator("#panel-mods")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#mods$/);
  });

  test("deep links and the legacy #updates alias resolve", async ({ page, backend }) => {
    await page.goto(entryURL(backend, "tags"));
    await expect(page.locator("#panel-tags")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#tags$/);

    // Legacy link: a fresh load of #updates lands on the queue panel and the
    // router rewrites the hash to #queue. (about:blank forces a full document
    // load — a same-document fragment jump only fires popstate, which does not
    // rewrite the URL; that is existing router behaviour, not a regression.)
    await page.goto("about:blank");
    await page.goto(entryURL(backend, "updates"));
    await expect(page.locator("#panel-queue")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#queue$/);

    // Unknown hash falls back to the dashboard instead of a blank page.
    await page.goto("about:blank");
    await page.goto(entryURL(backend, "does-not-exist"));
    await expect(page.locator("#panel-dashboard")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#dashboard$/);
  });

  test("sidebar quick links navigate too", async ({ page, backend }) => {
    await page.goto(entryURL(backend, "dashboard"));
    await navControl(page, "backups").click();
    await expect(page.locator("#panel-backups")).toHaveClass(/active/);
    await expect(page).toHaveURL(/#backups$/);
  });
});

test.describe("panel guard", () => {
  test("each panel element has exactly one active panel at a time", async ({ page, backend }) => {
    await gotoPanel(page, backend, "config");
    await expect(page.locator(".panel.active")).toHaveCount(1);
    await expect(page.locator(".panel.active")).toHaveAttribute("id", "panel-config");
  });
});
