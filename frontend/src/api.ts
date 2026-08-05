/**
 * REST API client for rwmod backend.
 */
import { clearToken, showLoginOverlay } from "./auth";

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

/**
 * fetch + JSON 的统一封装：非 2xx 抛 Error（优先用后端 detail），
 * 401 统一清除 token 并重新显示登录 overlay。
 */
export async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init);
  if (!resp.ok) {
    if (resp.status === 401) {
      clearToken();
      showLoginOverlay();
    }
    const body = await resp.json().catch(() => ({}));
    throw new Error((body as any).detail || `${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  return fetchJSON<T>(BASE + url, init);
}

export const api = {
  listMods: () => req<ModEntry[]>("/mods"),

  downloadMods: (ids: string[], force: boolean) =>
    req<{ total: number; results: DownloadResult[] }>("/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, force }),
    }),

  importCollection: (collectionId: string, force: boolean) =>
    req<{ total: number; results: DownloadResult[] }>("/import/collection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ collection_id: collectionId, force }),
    }),

  importFile: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{ total: number; results: DownloadResult[] }>("/import/file", {
      method: "POST",
      body: fd,
    });
  },

  importSort: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{
      total_packages: number;
      missing: number;
      unknown: string[];
      downloaded: number;
      results: DownloadResult[];
    }>("/import/sort", { method: "POST", body: fd });
  },

  getConfig: () => req<ConfigData>("/config"),

  saveConfig: (cfg: Partial<ConfigData>) =>
    req<{ ok: boolean }>("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(cfg),
    }),

  /** Export a .rwmod bundle (profiles/backups/tags/config) as a downloadable file. */
  exportBundle: async (includeBackups: boolean): Promise<void> => {
    const resp = await fetch(`${BASE}/transfer/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_backups: includeBackups }),
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
  importBundle: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return req<{ ok: boolean; msg: string; profiles: number; backups: number }>("/transfer/import", {
      method: "POST",
      body: fd,
    });
  },

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
        // 非 2xx（401 未认证 / 400 无效 ID）时按事件上报，避免下载永久挂起
        if (!resp.ok) {
          if (resp.status === 401) {
            clearToken();
            showLoginOverlay();
          }
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
}
