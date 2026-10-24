/**
 * Update check panel — compare local mod timestamps with Steam Workshop.
 * Supports one-click "Update All" via POST /api/auto-update/run.
 */
import { api, type UpdateItem } from "../api";
import { esc, escAttr } from "../dom";
import { toast } from "../toast";

export function initUpdatePanel() {
  document.getElementById("btn-check-updates")?.addEventListener("click", checkUpdates);
  document.getElementById("btn-update-all")?.addEventListener("click", updateAll);
}

async function checkUpdates() {
  const btn = document.getElementById("btn-check-updates") as HTMLButtonElement;
  const allBtn = document.getElementById("btn-update-all")!;
  const container = document.getElementById("update-results")!;
  btn.disabled = true;
  allBtn.style.display = "none";
  container.innerHTML = '<span style="color:var(--gray-text)">正在检查更新...</span>';

  try {
    const data = await api.checkUpdates();
    const updates: UpdateItem[] = data.updates || [];

    if (!updates.length) {
      container.innerHTML = '<span style="color:var(--green)">✅ 所有 Mod 均为最新版本</span>';
      return;
    }

    allBtn.style.display = "";
    renderUpdateList(updates, container);
  } catch (e: any) {
    container.innerHTML = `<span style="color:var(--red)">检查失败: ${esc(e.message)}</span>`;
  }
  btn.disabled = false;
}

function renderUpdateList(updates: UpdateItem[], container: HTMLElement) {
  container.innerHTML = updates
    .map(
      (u, i) => {
        const desc = (u.file_description || "").trim();
        const hasDesc = desc.length > 0;
        const preview = hasDesc ? desc.slice(0, 200) : "";
        const hasMore = desc.length > 200;
        const uid = `changelog-${i}`;

        return /* html */ `
    <div class="mod-row" style="align-items:flex-start">
      <div class="mod-icon" style="margin-top:4px">🔄</div>
      <div class="mod-info" style="flex:1;min-width:0">
        <div class="mod-name">${esc(u.name)}</div>
        <div class="mod-meta">
          <span>${esc(u.folder)}</span>
          <span>Workshop ${esc(u.workshop_id)}</span>
          ${u.time_updated ? `<span>更新于 ${esc(new Date(u.time_updated * 1000).toLocaleDateString("zh-CN"))}</span>` : ""}
        </div>
        ${hasDesc ? /* html */ `
        <div class="changelog-block" style="margin-top:8px;font-size:12px;color:var(--gray-text);line-height:1.5">
          <div class="changelog-preview" id="${uid}-preview">
            ${esc(preview)}${hasMore ? "..." : ""}
          </div>
          ${hasMore ? /* html */ `
          <div class="changelog-full" id="${uid}-full" style="display:none">
            ${esc(desc)}
          </div>
          <button class="btn btn-ghost btn-sm changelog-toggle" data-target="${uid}" style="margin-top:4px;font-size:11px">
            📖 展开全文
          </button>` : ""}
        </div>` : ""}
      </div>
      <button class="btn btn-primary btn-sm" data-id="${escAttr(u.workshop_id)}" style="flex-shrink:0">⬇ 更新</button>
    </div>`;
      },
    )
    .join("");

  // Changelog toggle
  container.querySelectorAll(".changelog-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const uid = (btn as HTMLElement).dataset.target!;
      const full = document.getElementById(`${uid}-full`)!;
      const preview = document.getElementById(`${uid}-preview`)!;
      if (full.style.display === "none") {
        full.style.display = "";
        preview.style.display = "none";
        (btn as HTMLElement).textContent = "📖 收起";
      } else {
        full.style.display = "none";
        preview.style.display = "";
        (btn as HTMLElement).textContent = "📖 展开全文";
      }
    });
  });

  container.querySelectorAll(".btn-primary[data-id]").forEach((b) => {
    b.addEventListener("click", async () => {
      const id = (b as HTMLElement).dataset.id!;
      try {
        // force=true: this is an update — without it the queue skips the mod
        // as already installed and the button silently does nothing (DL-12).
        const data = await api.addToQueue([id], true);
        toast(
          data.added > 0 ? `已加入队列并标记为更新: ${id}` : `已在队列中，已标记为更新: ${id}`,
          "success",
        );
      } catch (e: any) {
        toast(`失败: ${e.message}`, "error");
      }
    });
  });
}

async function updateAll() {
  const allBtn = document.getElementById("btn-update-all") as HTMLButtonElement;
  const container = document.getElementById("update-results")!;
  allBtn.disabled = true;
  allBtn.textContent = "⏳ 更新中...";
  container.innerHTML = '<span style="color:#7aa2f7">正在全部加入队列并开始下载...</span>';

  try {
    const data = await api.runAutoUpdate();
    if (data.ok === false) {
      // 已在运行：显示后端提示，避免 undefined
      const msg = data.msg || "更新检查已在运行中";
      toast(msg, "warn");
      container.innerHTML = `<span style="color:#e0af68">${esc(msg)}</span>`;
      allBtn.disabled = false;
      allBtn.textContent = "⬇ 全部更新";
      return;
    }
    // queued = ids add() actually accepted; the rest were already queued
    // (including residues just requeued by the restart recovery) and are
    // picked up by the start() call all the same.
    const queued = data.queued ?? 0;
    const outdated = data.outdated ?? 0;
    const requeued = Math.max(0, outdated - queued);
    toast(
      `已检查 ${data.checked} 个 Mod：${queued} 个新加入队列，${requeued} 个已在队列中，开始下载`,
      "success",
    );
    container.innerHTML =
      `<span style="color:#9ece6a">✅ 已开始处理 ${queued + requeued} 个待更新 Mod` +
      `（新加入 ${queued} 个，队列中已有 ${requeued} 个）</span>\n` +
      `<span style="color:var(--gray-text)">切换到 📋 队列 面板查看进度</span>`;
  } catch (e: any) {
    container.innerHTML = `<span style="color:#f7768e">全部更新失败: ${esc(e.message)}</span>`;
    toast(`更新失败: ${e.message}`, "error");
  }
  allBtn.disabled = false;
  allBtn.textContent = "⬇ 全部更新";
}
