/**
 * Mod settings slice — Config/Mod_*.xml inventory, the raw-XML editor and the
 * per-file backup list.
 *
 * The backend owns every safety rule (filename whitelist, 2 MB cap,
 * well-formedness, BOM/CRLF preservation, backup before write). The store only
 * forwards user intent and keeps the `ack_game_closed` contract explicit.
 */
import { create } from "zustand";

import { apiRequest, toMessage } from "../api/client";
import type { components } from "../api/generated";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

type ModSettingFile = components["schemas"]["ModSettingFile"];
type ModSettingContent = components["schemas"]["ModSettingContentResponse"];
type ModSettingBackup = components["schemas"]["ModSettingBackup"];
type ModSettingWriteResponse = components["schemas"]["ModSettingWriteResponse"];
type ModSettingRestoreResponse = components["schemas"]["ModSettingRestoreResponse"];

export interface SettingsEditorState {
  filename: string;
  xml: string;
  rootTag: string;
  size: number;
  mtime: string;
  gameRunning: boolean;
}

interface SettingsState {
  files: ModSettingFile[];
  configDir: string | null;
  backups: ModSettingBackup[];
  status: LoadStatus;
  error: string | null;
  editor: SettingsEditorState | null;
  editorLoading: boolean;
  saving: boolean;

  load: () => Promise<void>;
  loadList: () => Promise<void>;
  loadBackups: () => Promise<void>;
  open: (filename: string) => Promise<ModSettingContent>;
  closeEditor: () => void;
  save: (filename: string, xml: string, ackGameClosed: boolean) => Promise<ModSettingWriteResponse>;
  restore: (
    filename: string,
    backup: string,
    ackGameClosed: boolean,
  ) => Promise<ModSettingRestoreResponse>;
  backupAll: () => Promise<number>;
}

export const useSettingsStore = create<SettingsState>()((set, get) => ({
  files: [],
  configDir: null,
  backups: [],
  status: "idle",
  error: null,
  editor: null,
  editorLoading: false,
  saving: false,

  load: async () => {
    await Promise.all([get().loadList(), get().loadBackups()]);
  },

  loadList: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await apiRequest("get", "/api/mod-settings");
      set({
        files: data.files ?? [],
        configDir: data.config_dir ?? null,
        status: "ready",
        error: null,
      });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  loadBackups: async () => {
    try {
      const data = await apiRequest("get", "/api/mod-settings/backups");
      set({ backups: data.backups ?? [] });
    } catch {
      /* Backup listing is secondary; the editor still works without it. */
    }
  },

  open: async (filename) => {
    set({ editorLoading: true, error: null });
    try {
      const detail = (await apiRequest(
        "get",
        `/api/mod-settings/${encodeURIComponent(filename)}`,
      )) as ModSettingContent;
      set({
        editor: {
          filename: detail.filename ?? filename,
          xml: detail.xml ?? "",
          rootTag: detail.root_tag ?? "XML",
          size: detail.size ?? 0,
          mtime: detail.mtime ?? "",
          gameRunning: Boolean(detail.game_running),
        },
        editorLoading: false,
      });
      return detail;
    } catch (error) {
      set({ editorLoading: false, error: toMessage(error) });
      throw error;
    }
  },

  closeEditor: () => set({ editor: null }),

  save: async (filename, xml, ackGameClosed) => {
    set({ saving: true, error: null });
    try {
      const result = (await apiRequest(
        "put",
        `/api/mod-settings/${encodeURIComponent(filename)}`,
        { body: { xml, ack_game_closed: ackGameClosed } },
      )) as ModSettingWriteResponse;
      await Promise.all([get().loadList(), get().loadBackups()]);
      bump("settings");
      return result;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ saving: false });
    }
  },

  restore: async (filename, backup, ackGameClosed) => {
    set({ saving: true, error: null });
    try {
      const result = (await apiRequest(
        "post",
        `/api/mod-settings/${encodeURIComponent(filename)}/restore`,
        { body: { backup, ack_game_closed: ackGameClosed } },
      )) as ModSettingRestoreResponse;
      await Promise.all([get().loadList(), get().loadBackups()]);
      bump("settings");
      return result;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ saving: false });
    }
  },

  backupAll: async () => {
    try {
      const result = (await apiRequest("post", "/api/mod-settings/backup-all")) as {
        count?: number;
      };
      await get().loadBackups();
      bump("settings");
      return result.count ?? 0;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },
}));
