/**
 * Search panel — query Steam Workshop, add results to queue.
 */
import { api, type SearchHit } from "../api";
import { esc, escAttr } from "../dom";
import { toast } from "../toast";

export function initSearchPanel() {
  const input = document.getElementById("search-input") as HTMLInputElement | null;
  const btn = document.getElementById("btn-search") as HTMLButtonElement | null;

  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doSearch();
  });
  btn?.addEventListener("click", doSearch);
}

async function doSearch() {
  const input = document.getElementById("search-input") as HTMLInputElement;
  const q = input.value.trim();
  if (!q) return;

  const container = document.getElementById("search-results")!;
  container.innerHTML = '<span style="color:var(--gray-text)">搜索中...</span>';

  try {
    const data = await api.search(q);
    const results: SearchHit[] = data.results || [];

    if (!results.length) {
      container.innerHTML = '<span style="color:var(--gray-text)">无结果</span>';
      return;
    }

    container.innerHTML = results
      .map(
        (r) => /* html */ `
      <div class="search-hit">
        <div class="search-hit-header">
          <span class="search-hit-title">${esc(r.title)}</span>
          ${r.installed ? '<span style="color:var(--green);font-size:11px;font-weight:600">✓ 已安装</span>' : ""}
          <span class="search-hit-author">by ${esc(r.author)}</span>
          ${r.rating ? `<span class="search-hit-rating">⭐ ${esc(r.rating)}</span>` : ""}
          ${r.subscribers ? `<span class="search-hit-subs">${esc(r.subscribers)} 订阅</span>` : ""}
        </div>
        ${r.description ? `<div class="search-hit-desc">${esc(r.description)}</div>` : ""}
        <div style="margin-top:6px">
          <button class="btn btn-primary btn-sm" data-id="${escAttr(r.id)}">+ 加入队列</button>
        </div>
      </div>`,
      )
      .join("");

    container.querySelectorAll(".btn-primary[data-id]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = (btn as HTMLElement).dataset.id!;
        try {
          await api.addToQueue([id]);
          toast(`已加入队列: ${id}`, "success");
        } catch (e: any) {
          toast(`加入失败: ${e.message}`, "error");
        }
      });
    });
  } catch (e: any) {
    container.innerHTML = `<span style="color:var(--red)">搜索失败: ${esc(e.message)}</span>`;
  }
}
