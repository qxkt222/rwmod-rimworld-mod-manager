/**
 * Backups slice — mod backup inventory plus restore / delete / cleanup.
 *
 * The `.rwmod` transfer bundle also lives here: the ConfigPanel owns its UI,
 * but FE-2 may only add the five slice files in its task book, so the binary
 * export (the JSON client cannot carry blobs) is kept with the other
 * backup-shaped operations instead of leaking `fetch` into a component.
 */
import { create } from "zustand";

import { apiRequest, toMessage } from "../api/client";
import type { components } from "../api/generated";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

type BackupItem = components["schemas"]["BackupItem"];
type BackupRestoreResponse = components["schemas"]["BackupRestoreResponse"];
type BackupCleanupResponse = components["schemas"]["BackupCleanupResponse"];
type TransferImportResponse = { ok?: boolean; msg?: string; profiles?: number; backups?: number };

interface BackupsState {
  backups: BackupItem[];
  backupDir: string;
  status: LoadStatus;
  error: string | null;
  /** True while an export/import bundle transfer is in flight. */
  transferring: boolean;

  load: () => Promise<void>;
  restore: (workshopId: string, filename?: string) => Promise<BackupRestoreResponse>;
  remove: (filename: string) => Promise<void>;
  cleanup: (keep: number) => Promise<number>;
  exportBundle: (includeBackups: boolean, includeSecrets: boolean) => Promise<void>;
  importBundle: (file: File) => Promise<string>;
}

export const useBackupsStore = create<BackupsState>()((set, get) => ({
  backups: [],
  backupDir: "",
  status: "idle",
  error: null,
  transferring: false,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await apiRequest("get", "/api/backups");
      set({
        backups: data.backups ?? [],
        backupDir: data.backup_dir ?? "",
        status: "ready",
        error: null,
      });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  restore: async (workshopId, filename) => {
    const result = (await apiRequest("post", `/api/backups/${encodeURIComponent(workshopId)}/restore`, {
      body: filename ? { filename } : {},
    })) as BackupRestoreResponse;
    await get().load();
    bump("backups");
    bump("mods");
    bump("dashboard");
    return result;
  },

  remove: async (filename) => {
    await apiRequest("delete", `/api/backups/${encodeURIComponent(filename)}`);
    await get().load();
    bump("backups");
  },

  cleanup: async (keep) => {
    const result = (await apiRequest("post", "/api/backups/cleanup", {
      body: { keep },
    })) as BackupCleanupResponse;
    await get().load();
    bump("backups");
    return result.deleted ?? 0;
  },

  exportBundle: async (includeBackups, includeSecrets) => {
    set({ transferring: true, error: null });
    try {
      const response = await fetch("/api/transfer/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          include_backups: includeBackups,
          include_secrets: includeSecrets,
        }),
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
        throw new Error(
          typeof body.detail === "string" && body.detail.length > 0
            ? body.detail
            : `HTTP ${response.status}`,
        );
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      const disposition = response.headers.get("content-disposition") ?? "";
      anchor.href = url;
      anchor.download = /filename="?([^";]+)"?/.exec(disposition)?.[1] ?? "rwmod-backup.rwmod";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ transferring: false });
    }
  },

  importBundle: async (file) => {
    set({ transferring: true, error: null });
    try {
      const form = new FormData();
      form.append("file", file);
      const result = (await apiRequest("post", "/api/transfer/import", {
        form,
      })) as TransferImportResponse;
      bump("config");
      bump("backups");
      bump("profiles");
      bump("tags");
      const message = result.msg ?? "导入完成";
      return message;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ transferring: false });
    }
  },
}));
