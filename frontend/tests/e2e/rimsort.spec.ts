/**
 * RimSort panel — ModsConfig generation, uploaded-file comparison and load
 * order analysis. Everything runs against the real backend with an empty Mods
 * dir; no network or SteamCMD is involved.
 */
import { expect, gotoPanel, test } from "./helpers";

const COMPARE_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModsConfigData>
  <version>1.5.4104 rev435</version>
  <activeMods>
    <li>Ludeon.RimWorld</li>
    <li>SomeAuthor.TestMod</li>
    <li>AnotherAuthor.MissingMod</li>
  </activeMods>
</ModsConfigData>
`;

test.describe("RimSort panel", () => {
  test("generates ModsConfig.xml from the installed mods", async ({ page, backend }) => {
    await gotoPanel(page, backend, "rimsort");
    await page.locator("#btn-rimsort-generate").click();

    const output = page.locator("#rimsort-output");
    await expect(output).toBeVisible();
    await expect(output).toContainText("ModsConfigData");
    await expect(output).toContainText("activeMods");
  });

  test("uploaded ModsConfig.xml comparison renders installed/missing counts", async ({
    page,
    backend,
  }) => {
    await gotoPanel(page, backend, "rimsort");
    await page.locator("#rimsort-file").setInputFiles({
      name: "ModsConfig.xml",
      mimeType: "application/xml",
      buffer: Buffer.from(COMPARE_XML, "utf8"),
    });

    const result = page.locator("#rimsort-compare-result");
    await expect(result).toContainText("对比结果");
    await expect(result).toContainText("配置中有 3 个 Mod");
    await expect(result).toContainText("✅ 已安装 0");
    await expect(result).toContainText("❌ 缺失 2");
    await expect(result).toContainText("缺失的 Mod (2)");
    await expect(result).toContainText("SomeAuthor.TestMod");
    await expect(result).toContainText("无法解析 Workshop ID");
  });

  test("load order analysis flags the missing Harmony anchor", async ({ page, backend }) => {
    await gotoPanel(page, backend, "rimsort");
    await page.locator("#btn-rimsort-order").click();

    const result = page.locator("#rimsort-order-result");
    await expect(result).toContainText("排序分析");
    await expect(result).toContainText("1 个 Mod");
    await expect(result).toContainText("未检测到 Harmony");
    await expect(result).toContainText("1 警告");
  });
});
