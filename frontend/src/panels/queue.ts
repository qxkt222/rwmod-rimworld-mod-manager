/**
 * Queue panel — manage download queue, view progress bars.
 */
import { fetchJSON } from "../api";
import { connectWS } from "../ws";
import { refreshMods, setStatus } from "../main";

interface QueueItem {
  id: string;
  name: string;
  status: string;
  progress: number;
  msg: string;
}

let items: QueueItem[] = [];

export function initQueuePanel() {
  document.getElementById("btn-queue-start")?.addEventListener("click", startQueue);
  document.getElementById("btn-queue-clear")?.addEventListener("click", clearQueue);

  // Listen for WebSocket queue updates
  connectWS(() => {});
  import("../ws").then((m) => {
    m.addWSListener((msg) => {
      if (msg.type === "queue_update" && msg.items) {
        items = msg.items as QueueItem[];
        render();
      }
    });
  });

  refreshState();
}

async function refreshState() {
  try {
    const data = await fetchJSON<{ items: QueueItem[] }>("/api/queue");
    items = data.items || [];
    render();
  } catch {}
}

async function startQueue() {
  const btn = document.getElementById("btn-queue-start") as HTMLButtonElement;
  btn.disabled = true;
  setStatus("blue", "正在处理队列...");
  try {
    await fetchJSON("/api/queue/start", { method: "POST" });
    await refreshMods();
    setStatus("green", "队列完成");
  } catch {
    setStatus("red", "队列执行失败");
  }
  btn.disabled = false;
  refreshState();
}

async function clearQueue() {
  await fetchJSON("/api/queue/clear", { method: "POST" });
  refreshState();
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

  container.innerHTML = items
    .map(
      (i) => /* html */ `
    <div class="queue-item">
      <span class="q-status">${statusIcons[i.status] || "❓"}</span>
      <span class="q-id">${esc(i.id)}</span>
      <span class="q-name">${esc(i.name || "")}</span>
      <span class="q-progress">${esc(i.msg || i.status)}</span>
      ${i.status === "pending" ? `<button class="btn btn-ghost btn-sm" data-remove="${esc(i.id)}">✕</button>` : ""}
    </div>
    ${i.status === "downloading" ? `<div class="queue-bar"><div class="queue-bar-inner" style="width:${Math.round(i.progress * 100)}%"></div></div>` : ""}`,
    )
    .join("");

  container.querySelectorAll("[data-remove]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = (btn as HTMLElement).dataset.remove!;
      await fetchJSON(`/api/queue/${id}`, { method: "DELETE" });
      refreshState();
    });
  });
}

function esc(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
