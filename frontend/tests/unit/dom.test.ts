/**
 * Example component-layer test: the shared DOM helpers.
 *
 * This is the only unit test of wave 0a — it proves Vitest + happy-dom are
 * wired correctly. Panel behaviour is covered by Playwright E2E.
 */
import { describe, expect, it } from "vitest";
import { esc, escAttr, fmtBytes } from "../../src/dom";

describe("dom helpers", () => {
  it("esc escapes text content for HTML text positions", () => {
    // Quotes intentionally stay literal in text positions.
    expect(esc('<img src=x onerror="alert(1)">')).toBe('&lt;img src=x onerror="alert(1)"&gt;');
    expect(esc("Tom & Jerry")).toBe("Tom &amp; Jerry");
    expect(esc("plain text")).toBe("plain text");
  });

  it("escAttr escapes every attribute metacharacter exactly once", () => {
    expect(escAttr('a&b<c>d"e\'f')).toBe("a&amp;b&lt;c&gt;d&quot;e&#39;f");
    // Already-escaped entities must not double-encode the leading ampersand.
    expect(escAttr("&lt;")).toBe("&amp;lt;");
  });

  it("fmtBytes renders human-readable sizes", () => {
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(1024 * 1024 * 3)).toBe("3.0 MB");
  });
});
