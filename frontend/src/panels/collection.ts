/**
 * Collection panel — download entire Steam Workshop collections.
 * Uses the SSE download stream so per-batch progress is shown live instead
 * of one line followed by a long silent wait.
 */
import { api, fetchJSON } from "../api";
import { setStatus, refreshMods } from "../main";

const EVENT_COLORS: Record<string, string> = {
  start: "#7aa2f7",
  info: "#7aa2f7",
  line: "#9ece6a",
  skip: "#e0af68",
  ok: "#9ece6a",
  warn: "#f7768e",
  fail: "#f7768e",
  done: "#9ece6a",
};

export function initCollectionPanel() {
  const input = document.getElementById("collection-input") as HTMLInputElement | null;
  const btn = document.getElementById("btn-collection") as HTMLButtonElement | null;
  const previewBtn = document.getElementById("btn-collection-preview");

  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") startCollection();
  });
  btn?.addEventListener("click", startCollection);
  previewBtn?.addEventListener("click", previewCollection);
}

const EVENT_PREFIX: Record<string, string> = {
  ok: "  ✓ ",
  warn: "  ✗ ",
  skip: "  ⏭ ",
};

function logLine(log: HTMLElement, text: string, event: string) {
  const color = EVENT_COLORS[event] || "#c0caf5";
  const prefix = EVENT_PREFIX[event] || "";
  log.innerHTML += `<span style="color:${color}">${esc(prefix + text)}</span>\n`;
  log.scrollTop = log.scrollHeight;
}

// ── live progress bar state ────────────────────────────────────
let totalCount = 0;      // mods to download (from backend 'total')
let doneCount = 0;       // ok + warn + skip so far
let current: Record<string, { percent: number; downloaded: number; total: number; ts: number }> = {};

function fmtBytes(n: number): string {
  if (!n || n <= 0 || !isFinite(n)) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 ? v.toFixed(0) : v.toFixed(1)} ${units[i]}`;
}

function renderProgress() {
  const box = document.getElementById("collection-progress");
  if (!box) return;
  const entries = Object.values(current);
  const show = totalCount > 0 || entries.length > 0;
  box.style.display = show ? "block" : "none";
  if (!show) return;

  const pct = totalCount > 0 ? Math.min(100, Math.round((doneCount / totalCount) * 100)) : 0;
  const bar = document.getElementById("cp-total-bar");
  const count = document.getElementById("cp-count");
  const cur = document.getElementById("cp-current");
  if (bar) bar.style.width = `${pct}%`;
  if (count) count.textContent = `${doneCount} / ${totalCount} (${pct}%)`;
  if (cur) {
    if (!entries.length) {
      cur.textContent = "";
      return;
    }
    // Speed: derive from the last two samples of each active download.
    const parts = entries.map((e) => {
      let speed = "";
      if (e.ts && e.downloaded > 0) {
        speed = ` · ${fmtBytes(e.downloaded)} / ${fmtBytes(e.total)}`;
      }
      return `${e.percent.toFixed(1)}%${speed}`;
    });
    cur.textContent = `下载中: ${entries.map((e, i) => `${Object.keys(current)[i]} ${parts[i]}`).join("  |  ")}`;
  }
}

async function startCollection() {
  const input = document.getElementById("collection-input") as HTMLInputElement;
  const forceChk = document.getElementById("force-collection") as HTMLInputElement;
  const cid = input.value.trim();
  if (!cid) return;

  const log = document.getElementById("log-collection")!;
  log.innerHTML = `<span style="color:#7aa2f7">正在获取合集 ${esc(cid)}...</span>\n`;
  setStatus("blue", "正在获取合集...");

  // Reset progress state
  totalCount = 0;
  doneCount = 0;
  current = {};

  let finished = false;
  // SSE stream: the backend detects the collection, fetches its children and
  // downloads them in batches — live byte progress arrives as 'progress'
  // events and batch results as ok/warn.
  api.downloadStream(cid, forceChk.checked, (evt) => {
    if (evt.event === "info" && typeof evt.total === "number") {
      totalCount = evt.total;
    } else if (evt.event === "progress" && evt.id) {
      current[evt.id] = {
        percent: evt.percent ?? 0,
        downloaded: evt.downloaded ?? 0,
        total: evt.total ?? 0,
        ts: Date.now(),
      };
      renderProgress();
      return;
    } else if (evt.event === "ok" || evt.event === "warn" || evt.event === "skip") {
      doneCount += 1;
      if (evt.event !== "skip") delete current[evt.id];
      renderProgress();
    } else if (evt.event === "done") {
      finished = true;
      totalCount = 0;
      current = {};
      renderProgress();
      setStatus("green", "合集下载完成");
      refreshMods();
      return;
    } else if (evt.event === "fail") {
      finished = true;
      totalCount = 0;
      current = {};
      renderProgress();
      setStatus("red", "合集下载失败");
      refreshMods();
      return;
    }
    if (evt.msg) logLine(log, evt.msg, evt.event);
  });

  // Timeout guard: if the stream ends without a done/fail event (network
  // drop), surface it instead of leaving a dangling "获取中..." state.
  setTimeout(() => {
    if (!finished) {
      logLine(log, "⚠ 连接中断（下载可能在后台继续，可刷新查看）", "warn");
      setStatus("red", "合集下载连接中断");
      refreshMods();
    }
  }, 30 * 60 * 1000);
}

async function previewCollection() {
  const input = document.getElementById("collection-input") as HTMLInputElement;
  const cid = input.value.trim();
  if (!cid) return;
  const log = document.getElementById("log-collection")!;
  log.innerHTML = '<span style="color:#7aa2f7">正在获取合集预览...</span>\n';

  try {
    const d = await fetchJSON<{
      error?: string;
      collection_id?: string;
      total?: number;
      installed_count?: number;
      new_count?: number;
      failed_count?: number;
    }>(`/api/collection/preview/${encodeURIComponent(cid)}`);
    if (d.error) { log.innerHTML = `<span style="color:#f7768e">${esc(d.error)}</span>`; return; }
    log.innerHTML =
      `<span style="color:#9ece6a">合集 ${esc(String(d.collection_id ?? cid))}: ${d.total} 个 Mod</span>\n` +
      `<span style="color:#9ece6a">  🟢 已安装 ${d.installed_count}</span>\n` +
      `<span style="color:#7aa2f7">  🔵 新发现 ${d.new_count}</span>\n` +
      (d.failed_count ? `<span style="color:#e0af68">  🟡 之前失败 ${d.failed_count}</span>\n` : "") +
      `<span style="color:var(--gray-text)">点击"下载合集"开始下载</span>\n`;
    log.scrollTop = log.scrollHeight;
  } catch (e: any) { log.innerHTML = `<span style="color:#f7768e">预览失败: ${e.message}</span>`; }
}

function esc(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
