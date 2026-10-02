/**
 * Shared DOM helpers.
 *
 * These five functions used to be copy-pasted into every panel: esc() existed
 * in 15 files, fmtBytes() in 3, formatTs() in 2 and setText() in 1. The two
 * escAttr() copies had actually drifted — the profiles.ts one forgot `&`, so
 * attribute values written from it came back corrupted on round-trip. One
 * implementation makes that class of drift impossible.
 */

/**
 * Escape *text* for interpolation into HTML text content.
 * Quotes are left alone on purpose — use escAttr() for attribute values.
 */
export function esc(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

/**
 * Escape a value for interpolation into an HTML attribute value
 * (double- or single-quoted). Escapes `& < > " '`; the leading `&` pass is
 * required so already-escaped entities don't double-encode.
 */
export function escAttr(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Human-readable byte size (B/KB/MB/GB), e.g. 1536 → "1.5 KB". */
export function fmtBytes(n: number): string {
  if (!n || n <= 0 || !isFinite(n)) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

/** ISO timestamp → zh-CN local string; returns the input unchanged if unparsable. */
export function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN");
  } catch {
    return iso;
  }
}

/** Set an element's textContent by id. No-op when the element is absent. */
export function setText(id: string, text: string): void {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}
