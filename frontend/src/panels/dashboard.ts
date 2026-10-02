/**
 * Dashboard panel — stats, recent activity, quick actions including 一键更新.
 */
import { api, type DashboardData, type QueueItem } from "../api";
import { esc, setText } from "../dom";
import { toast } from "../toast";

let queuePollTimer: ReturnType<typeof setInterval> | null = null;

export function initDashboardPanel(): void {
  loadDashboard();
  bindAutoUpdate();
}

async function loadDashboard(): Promise<void> {
  try {
    const d = await api.getDashboard();

    setText("db-mods-count", String(d.mods_count));
    setText("db-updates-count", String(d.updates_pending));
    setText("db-disk-usage", formatSize(d.disk_usage_mb));

    const list = document.getElementById("db-activity")!;
    if (!d.recent_activity?.length) {
      list.innerHTML = '<span style="color:var(--gray-text)">暂无活动记录</span>';
    } else {
      list.innerHTML = d.recent_activity.slice(0, 6)
        .map(a => /* html */ `
          <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
            <span>${a.status === "success" ? "✅" : "❌"}</span>
            <span style="font-weight:600">${esc(a.mod_name || a.workshop_id)}</span>
            <span style="color:var(--gray-text);margin-left:auto">${a.created_at || ""}</span>
          </div>
        `).join("");
    }
  } catch { /* ignore */ }
}

// ── 一键更新 ──────────────────────────────────────────────────────

function bindAutoUpdate(): void {
  const btn = document.getElementById("btn-auto-update-dashboard") as HTMLButtonElement | null;
  if (!btn) return;

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "⏳ 检查更新中...";

    try {
      const data = await api.runAutoUpdate();

      if (data.ok === false) {
        // 已在运行：显示后端给出的提示，避免 undefined
        const msg = data.msg || "更新检查已在运行中";
        btn.textContent = `⏳ ${msg}`;
        toast(msg, "info");
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 3000);
        return;
      }

      if (data.outdated === 0) {
        btn.textContent = "✅ 全部最新";
        toast("所有 Mod 均为最新版本", "success");
        setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 3000);
        return;
      }

      btn.textContent = `✅ ${data.queued} 个已入队 · 监控中...`;
      toast(`发现 ${data.outdated} 个待更新 Mod，已加入下载队列`, "success");

      // Poll queue progress
      pollQueueProgress(btn, original);
    } catch (e: any) {
      btn.textContent = "❌ 失败，重试";
      toast(`检查失败: ${e.message}`, "error");
      setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 3000);
    }
  });
}

/** Poll /api/queue every 2s until all items are done/failed, then restore button. */
function pollQueueProgress(btn: HTMLButtonElement, original: string | null): void {
  stopQueuePolling();
  queuePollTimer = setInterval(async () => {
    try {
      const data = await api.getQueue();
      const items = data.items || [];

      const active = items.filter(i => i.status === "pending" || i.status === "downloading");
      const done = items.filter(i => i.status === "done").length;
      const failed = items.filter(i => i.status === "failed").length;

      if (active.length === 0) {
        // 队列完成或为空都结束轮询，避免 interval 泄漏
        stopQueuePolling();
        btn.disabled = false;
        if (items.length === 0) {
          btn.textContent = original ?? "⬇ 一键更新";
        } else {
          const total = done + failed;
          btn.textContent = failed > 0
            ? `⚠ ${done}/${total} 完成 (${failed} 失败)`
            : `✅ ${total} 个已完成`;
        }
        setTimeout(() => { btn.textContent = original; }, 5000);
        loadDashboard(); // refresh stats
      } else {
        btn.textContent = `⏳ ${done}/${items.length} 完成...`;
      }
    } catch {
      // keep polling
    }
  }, 2000);
}

/** 停止队列进度轮询（面板切换时调用，避免 setInterval 泄漏）。 */
export function stopQueuePolling(): void {
  if (queuePollTimer) {
    clearInterval(queuePollTimer);
    queuePollTimer = null;
  }
}

// ── helpers ───────────────────────────────────────────────────────

function formatSize(mb: number): string {
  return mb < 1024 ? `${mb} MB` : `${(mb / 1024).toFixed(1)} GB`;
}
