/**
 * Queue panel — manage download queue, view progress bars.
 */
import { api, type QueueItem } from "../api";
import { esc, escAttr, fmtBytes } from "../dom";
import { addWSListener, connectWS } from "../ws";
import { refreshMods, setStatus } from "../main";

// P0-2: the snapshot now carries a structured failure reason. api.ts is a
// shared file, so extend the type locally instead of editing it.
type QueueItemWithError = QueueItem & { error_kind?: string; error_detail?: string };

let items: QueueItemWithError[] = [];
// P2-1: pause is process-local state exposed by GET /api/queue.
let paused = false;

// Hover detail is one short SteamCMD/install line, never a log dump.
const MAX_ERROR_DETAIL = 200;

/** Create the pause button next to "开始" without touching index.html. */
function ensurePauseButton(): HTMLButtonElement | null {
  let btn = document.getElementById("btn-queue-pause") as HTMLButtonElement | null;
  if (btn) return btn;
  const start = document.getElementById("btn-queue-start");
  if (!start || !start.parentElement) return null;
  btn = document.createElement("button");
  btn.id = "btn-queue-pause";
  btn.className = "btn btn-ghost btn-sm";
  btn.textContent = "⏸ 暂停";
  start.parentElement.insertBefore(btn, start.nextSibling);
  return btn;
}

function renderPauseState() {
  const btn = ensurePauseButton();
  if (!btn) return;
  btn.textContent = paused ? "▶ 恢复" : "⏸ 暂停";
  btn.title = paused ? "继续处理队列" : "当前批次完成后暂停";
}

export function initQueuePanel() {
  document.getElementById("btn-queue-start")?.addEventListener("click", startQueue);
  document.getElementById("btn-queue-clear")?.addEventListener("click", clearQueue);
  ensurePauseButton()?.addEventListener("click", togglePause);
  renderPauseState();

  // Listen for WebSocket queue updates
  connectWS();
  addWSListener((msg) => {
    if (msg.type === "queue_update" && msg.items) {
      items = msg.items;
      render();
    }
  });

  refreshState();
}

async function refreshState() {
  try {
    // api.getQueue() predates the paused field; widen locally instead of
    // changing the shared api.ts export.
    const data = (await api.getQueue()) as {
      items: QueueItemWithError[];
      paused?: boolean;
    };
    items = data.items || [];
    paused = !!data.paused;
    render();
  } catch {}
}

async function togglePause() {
  try {
    if (paused) {
      await api.resumeQueue();
      setStatus("green", "队列已恢复");
    } else {
      await api.pauseQueue();
      setStatus("yellow", "队列已暂停（当前批次完成后停止）");
    }
    await refreshState();
  } catch {
    setStatus("red", "队列状态切换失败");
  }
}

async function startQueue() {
  const btn = document.getElementById("btn-queue-start") as HTMLButtonElement;
  btn.disabled = true;
  setStatus("blue", "正在处理队列...");
  try {
    await api.startQueue();
    await refreshMods();
    setStatus("green", "队列完成");
  } catch {
    setStatus("red", "队列执行失败");
  }
  btn.disabled = false;
  refreshState();
}

async function clearQueue() {
  await api.clearQueue();
  refreshState();
}

function fmtSpeed(bps: number): string {
  if (!bps || bps <= 0 || !isFinite(bps)) return "";
  return `${fmtBytes(bps)}/s`;
}

function render() {
  const container = document.getElementById("queue-list")!;
  if (!items.length) {
    container.innerHTML = '<div style="padding:16px;text-align:center;color:var(--gray-text)">队列为空，去「搜索」或「下载」添加 Mod</div>';
    return;
  }

  const statusIcons: Record<string, string> = {
    pending: "⏳",
    downloading: "⬇️",
    done: "✅",
    failed: "❌",
    cancelled: "🚫",
  };

  // Rows the user can act on: cancel an in-flight download, drop a queued or
  // failed one. Without this, a stuck 'downloading'/'failed' row had no ✕ and
  // the queue could only be rescued by clearing everything (DL-4 fallout).
  const removable = ["pending", "downloading", "failed"];

  container.innerHTML = items
    .map((i) => {
      const downloading = i.status === "downloading";
      const pct = Math.round(i.progress * 100);
      const speed = downloading ? fmtSpeed(i.speed_bps || 0) : "";
      const bytes =
        downloading && (i.downloaded || 0) > 0
          ? `${fmtBytes(i.downloaded!)} / ${fmtBytes(i.total!)}`
          : "";
      const detailBits = [speed, bytes].filter(Boolean).join(" · ");
      // Short reason (msg) stays visible; the raw detail is hover-only and
      // truncated, so a long SteamCMD line never bloats the row.
      const errorDetail = (i.error_detail || "").trim().slice(0, MAX_ERROR_DETAIL);
      const errorTitle = errorDetail ? ` title="${escAttr(errorDetail)}"` : "";
      return /* html */ `
    <div class="queue-item">
      <span class="q-status">${statusIcons[i.status] || "❓"}</span>
      <span class="q-id">${esc(i.id)}</span>
      <span class="q-name">${esc(i.name || "")}</span>
      <span class="q-progress"${errorTitle}>${esc(i.msg || i.status)}${detailBits ? ` <span class="q-speed">${esc(detailBits)}</span>` : ""}</span>
      ${i.status === "failed" ? `<button class="btn btn-ghost btn-sm" data-retry="${escAttr(i.id)}" title="重新排队">↻</button>` : ""}
      ${removable.includes(i.status) ? `<button class="btn btn-ghost btn-sm" data-remove="${escAttr(i.id)}">✕</button>` : ""}
    </div>
    ${downloading ? `<div class="queue-bar"><div class="queue-bar-inner" style="width:${pct}%"></div></div>
    <div class="queue-meta">${pct}%${detailBits ? ` — ${esc(detailBits)}` : ""}</div>` : ""}`;
    })
    .join("");

  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = (btn as HTMLElement).dataset.remove!;
      await api.removeQueueItem(id);
      refreshState();
    });
  });

  // P2-1: one-click retry for a single failed item.
  container.querySelectorAll("[data-retry]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = (btn as HTMLElement).dataset.retry!;
      try {
        await api.retryQueue([id]);
        setStatus("blue", `已重新排队: ${id}`);
      } catch {
        setStatus("red", "重试失败");
      }
      refreshState();
    });
  });

  renderPauseState();
}
