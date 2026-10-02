/**
 * REST API client for rwmod backend — the single, typed HTTP exit of the UI.
 *
 * Panels used to call bare `fetchJSON("/api/...")`/`fetch(...)` in 30+ places,
 * which is how POST /api/queue/add ended up copy-pasted verbatim into three
 * panels and why response shapes could drift per panel. Every endpoint the UI
 * talks to now has a method here; `fetchJSON` is module-private on purpose.
 */

const BASE = "/api";

export interface ModEntry {
  folder: string;
  name: string;
  package_id: string;
  workshop_id: string;
}

export interface ConfigData {
  steamcmd_path: string;
  mods_dir: string;
  rimworld_dir: string;
  backup_dir: string;
  /** Whether a Steam API key is configured (the raw key is never exposed). */
  has_steam_api_key: boolean;
  steamcmd_exists: boolean;
  mods_dir_exists: boolean;
}

export interface DownloadResult {
  id: string;
  ok: boolean;
}

// ── queue / history / auto-update ────────────────────────────────

export interface QueueItem {
  id: string;
  name: string;
  status: string;
  progress: number;
  msg: string;
  downloaded?: number;
  total?: number;
  speed_bps?: number;
}

export interface HistoryItem {
  id: number;
  workshop_id: string;
  mod_name: string;
  package_id: string;
  status: string;
  msg: string;
  created_at: string;
}

export interface HistoryStats {
  total: number;
  success: number;
  failed: number;
}

export interface AutoUpdateResult {
  ok?: boolean;
  msg?: string;
  checked?: number;
  outdated?: number;
  queued?: number;
}

// ── mods ─────────────────────────────────────────────────────────

export interface ModHealthEntry {
  folder: string;
  status: string;
}

export interface ModCompatibility {
  rimworld_version?: string | null;
  groups?: {
    incompatible?: { folder: string }[];
    unknown?: { folder: string }[];
  };
}

export interface ModDependency {
  id?: string;
  name?: string;
  installed?: boolean;
}

export interface ModsExport {
  exported_at: string;
  total: number;
  mods: ModEntry[];
}

export interface ExportCollectionResult {
  total: number;
  mods: { workshop_id: string; name: string; url: string }[];
  ids: string[];
  markdown: string;
}

export interface CollectionPreview {
  collection_id?: string;
  total?: number;
  installed_count?: number;
  new_count?: number;
  failed_count?: number;
}

// ── updates / search / saves / tags ──────────────────────────────

export interface UpdateItem {
  workshop_id: string;
  name: string;
  folder: string;
  remote_title: string;
  time_updated: number;
  file_description: string;
}

export interface SearchHit {
  id: string;
  title: string;
  author: string;
  description: string;
  preview_url: string;
  rating: string;
  subscribers: string;
  installed: boolean;
}

export interface SaveEntry {
  name: string;
  game_version?: string;
  total_mods: number;
  missing_count: number;
  loadable: boolean;
  completeness?: number;
}

export interface TagInfo {
  tag: string;
  count: number;
}

// ── backups / profiles / rimsort / dashboard ─────────────────────

export interface BackupEntry {
  filename: string;
  workshop_id: string;
  folder_name: string;
  timestamp: string;
  size_mb: number;
}

export interface BackupList {
  backups: BackupEntry[];
  backup_dir: string;
}

export interface ProfileEntry {
  name: string;
  mod_count: number;
  saved_at: string;
  size_kb: number;
}

export interface RimsortCompareResult {
  /** Legacy parse-failure field still tolerated by the UI. */
  error?: string;
  total_in_config?: number;
  installed?: string[];
  missing?: string[];
  extra?: string[];
  missing_details?: { workshop_id?: string }[];
}

export interface LoadOrderResult {
  /** Legacy failure field still tolerated by the UI. */
  error?: string;
  total_mods?: number;
  issues?: { severity: string; message: string }[];
  load_order?: string[];
  ok?: boolean;
}

export interface DashboardData {
  mods_count: number;
  updates_pending: number;
  disk_usage_mb: number;
  recent_activity: { workshop_id: string; mod_name: string; status: string; created_at: string }[];
}

// ── plumbing ─────────────────────────────────────────────────────

/** fetch + JSON 的统一封装：非 2xx 抛 Error（优先用后端 detail）。 */
async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error((body as any).detail || `${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  return fetchJSON<T>(BASE + url, init);
}

function postJSON<T>(url: string, body: unknown): Promise<T> {
  return req<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function postForm<T>(url: string, file: File): Promise<T> {
  const fd = new FormData();
  fd.append("file", file);
  return req<T>(url, { method: "POST", body: fd });
}

export const api = {
  // ── mods ───────────────────────────────────────────────────────
  listMods: () => req<ModEntry[]>("/mods"),

  getModHealth: () => req<{ mods?: ModHealthEntry[] }>("/mods/health"),

  getModCompatibility: () => req<ModCompatibility>("/mods/compatibility"),

  getModDependencies: (ids: string[]) =>
    postJSON<{ deps?: Record<string, ModDependency[]> }>("/mods/dependencies", { ids }),

  exportMods: () => req<ModsExport>("/mods/export"),

  exportCollection: () => req<ExportCollectionResult>("/mods/export-collection"),

  checkUpdates: () => req<{ updates?: UpdateItem[] }>("/mods/check-updates"),

  // ── download / collection ──────────────────────────────────────
  downloadMods: (ids: string[], force: boolean) =>
    postJSON<{ total: number; results: DownloadResult[] }>("/download", { ids, force }),

  importCollection: (collectionId: string, force: boolean) =>
    postJSON<{ total: number; results: DownloadResult[] }>("/import/collection", {
      collection_id: collectionId,
      force,
    }),

  importFile: (file: File) =>
    postForm<{ total: number; results: DownloadResult[] }>("/import/file", file),

  importSort: (file: File) =>
    postForm<{
      total_packages: number;
      missing: number;
      unknown: string[];
      downloaded: number;
      results: DownloadResult[];
    }>("/import/sort", file),

  previewCollection: (collectionId: string) =>
    req<CollectionPreview>(`/collection/preview/${encodeURIComponent(collectionId)}`),

  // ── queue ──────────────────────────────────────────────────────
  getQueue: () => req<{ items: QueueItem[] }>("/queue"),

  /**
   * Add mod ids (or Workshop URLs) to the download queue.
   * Single implementation — search/updates/rimsort used to duplicate the
   * POST/header/body verbatim.
   */
  addToQueue: (ids: string[]) =>
    postJSON<{ added: number; items: { id: string; status: string }[] }>("/queue/add", { ids }),

  startQueue: () => req<{ ok: boolean }>("/queue/start", { method: "POST" }),

  clearQueue: () => req<{ ok: boolean }>("/queue/clear", { method: "POST" }),

  removeQueueItem: (id: string) =>
    req<{ ok: boolean }>(`/queue/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // ── history ────────────────────────────────────────────────────
  getHistory: (limit = 50) => req<{ items: HistoryItem[] }>(`/history?limit=${limit}`),

  getHistoryStats: () => req<HistoryStats>("/history/stats"),

  clearHistory: () => req<{ ok: boolean }>("/history/clear", { method: "POST" }),

  // ── auto-update ────────────────────────────────────────────────
  runAutoUpdate: () => req<AutoUpdateResult>("/auto-update/run", { method: "POST" }),

  getAutoUpdateStatus: () => req<{ running: boolean }>("/auto-update/status"),

  // ── backups ────────────────────────────────────────────────────
  listBackups: () => req<BackupList>("/backups"),

  deleteBackup: (filename: string) =>
    req<{ ok: boolean }>(`/backups/${encodeURIComponent(filename)}`, { method: "DELETE" }),

  restoreBackup: (workshopId: string, filename?: string) =>
    postJSON<{ ok?: boolean; msg?: string; restored_folder?: string }>(
      `/backups/${encodeURIComponent(workshopId)}/restore`,
      filename ? { filename } : {},
    ),

  cleanupBackups: (keep: number) =>
    postJSON<{ ok: boolean; deleted: number }>("/backups/cleanup", { keep }),

  // ── profiles ───────────────────────────────────────────────────
  listProfiles: () =>
    req<{ profiles?: ProfileEntry[]; modsconfig_path?: string | null }>("/profiles"),

  saveProfile: (name: string) =>
    postJSON<{ ok?: boolean; msg?: string }>("/profiles/save", { name }),

  restoreProfile: (name: string) =>
    req<{ ok?: boolean; msg?: string; mod_count?: number }>(
      `/profiles/${encodeURIComponent(name)}/restore`,
      { method: "POST" },
    ),

  deleteProfile: (name: string) =>
    req<{ ok?: boolean }>(`/profiles/${encodeURIComponent(name)}`, { method: "DELETE" }),

  // ── rimsort ────────────────────────────────────────────────────
  generateRimsort: () => req<{ modsconfig_xml?: string }>("/rimsort/generate", { method: "POST" }),

  compareRimsortFile: (file: File) => postForm<RimsortCompareResult>("/rimsort/compare-file", file),

  checkLoadOrder: () => req<LoadOrderResult>("/rimsort/check-order"),

  // ── search / saves / tags ──────────────────────────────────────
  search: (q: string) => req<{ results?: SearchHit[] }>(`/search?q=${encodeURIComponent(q)}`),

  listSaves: () => req<{ saves?: SaveEntry[] }>("/saves"),

  analyzeSave: (file: File) =>
    postForm<{ filename?: string; mod_count?: number }>("/saves/analyze", file),

  listTags: () => req<{ tags?: TagInfo[] }>("/tags"),

  getTagFolders: (tag: string) =>
    req<{ tag: string; folders?: string[] }>(`/tags/by-tag/${encodeURIComponent(tag)}`),

  // ── config / dashboard / status ────────────────────────────────
  getConfig: () => req<ConfigData>("/config"),

  saveConfig: (cfg: Partial<ConfigData>) =>
    postJSON<{ ok: boolean }>("/config", cfg),

  checkSteamcmd: () => req<{ ok?: boolean; msg?: string }>("/steamcmd/check"),

  getDashboard: () => req<DashboardData>("/dashboard"),

  getStatus: () => req<{ online: boolean }>("/status"),

  // ── transfer / undo ────────────────────────────────────────────
  /** Export a .rwmod bundle (profiles/backups/tags/config) as a downloadable file. */
  exportBundle: async (includeBackups: boolean, includeSecrets = false): Promise<void> => {
    const resp = await fetch(`${BASE}/transfer/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_backups: includeBackups, include_secrets: includeSecrets }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error((body as any).detail || `${resp.status} ${resp.statusText}`);
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = resp.headers.get("content-disposition")?.match(/filename="?([^";]+)"?/)?.[1] || "rwmod-backup.rwmod";
    a.click();
    URL.revokeObjectURL(url);
  },

  /** Import a .rwmod bundle, restoring profiles/backups/tags/config. */
  importBundle: (file: File) =>
    postForm<{ ok: boolean; msg: string; profiles: number; backups: number }>(
      "/transfer/import",
      file,
    ),

  /** List available undo snapshots of ModsConfig.xml. */
  listUndo: () => req<{ snapshots: { name: string; size_kb: number }[]; modsconfig_path: string }>("/undo"),

  /** Restore the most recent pre-operation snapshot of ModsConfig.xml. */
  undoLast: () =>
    req<{ ok: boolean; msg: string; restored: string | null }>("/undo", {
      method: "POST",
    }),

  /** SSE download stream — returns an AbortController + async generator */
  downloadStream(
    id: string,
    force: boolean,
    onEvent: (evt: SSEEvent) => void,
  ): AbortController {
    const ctrl = new AbortController();
    const url = `${BASE}/download/stream?id=${encodeURIComponent(id)}&force=${force}`;

    fetch(url, { signal: ctrl.signal })
      .then(async (resp) => {
        // 非 2xx（400 无效 ID 等）时按事件上报，避免下载永久挂起
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          onEvent({ event: "fail", msg: (body as any).detail || `HTTP ${resp.status}` });
          return;
        }
        const reader = resp.body?.getReader();
        if (!reader) return;
        const dec = new TextDecoder();
        let buf = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop()!;
          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                onEvent(JSON.parse(line.slice(6)));
              } catch { /* ignore bad JSON */ }
            }
          }
        }
      })
      .catch(() => {
        // 主动 abort（超时兜底）时不重复上报；网络错误则上报 fail
        if (!ctrl.signal.aborted) {
          onEvent({ event: "fail", msg: "连接中断，下载中止" });
        }
      });

    return ctrl;
  },
};

export interface SSEEvent {
  event: string;
  id?: string;
  msg?: string;
  line?: string;
  mod_id?: string;
  /** Collection total (info event) or per-mod byte totals (progress event). */
  total?: number;
  /** Live download progress (progress event). */
  percent?: number;
  downloaded?: number;
}
