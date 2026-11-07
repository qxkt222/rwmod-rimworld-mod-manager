/**
 * Profiles slice (FE-3 wave 2) — ModsConfig.xml snapshots plus undo snapshots.
 *
 * Mutations bump the `profiles` topic so every mounted consumer refreshes;
 * restore/undo also bump `mods` because the active ModsConfig.xml changes
 * underneath the mods/rimsort panels (docs/ui-redesign.md §4.1).
 */
import { create } from "zustand";

import { request, toMessage } from "../api/client";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

export interface ProfileEntry {
  name: string;
  mod_count?: number;
  saved_at?: string;
  size_kb?: number;
}

export interface UndoSnapshot {
  name: string;
  size_kb: number;
}

export interface ProfileSaveResult {
  ok?: boolean;
  msg?: string;
  mod_count?: number;
}

export interface UndoResult {
  ok: boolean;
  msg: string;
  restored?: string | null;
}

interface ProfilesState {
  profiles: ProfileEntry[];
  modsconfigPath: string | null;
  undoSnapshots: UndoSnapshot[];
  status: LoadStatus;
  error: string | null;
  undoStatus: LoadStatus;
  undoError: string | null;

  load: () => Promise<void>;
  loadUndo: () => Promise<void>;
  save: (name: string) => Promise<ProfileSaveResult>;
  restore: (name: string) => Promise<ProfileSaveResult>;
  remove: (name: string) => Promise<void>;
  undo: () => Promise<UndoResult>;
}

export const useProfilesStore = create<ProfilesState>()((set) => ({
  profiles: [],
  modsconfigPath: null,
  undoSnapshots: [],
  status: "idle",
  error: null,
  undoStatus: "idle",
  undoError: null,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await request<{
        profiles?: ProfileEntry[];
        modsconfig_path?: string | null;
      }>("get", "/api/profiles");
      set({
        profiles: data.profiles ?? [],
        modsconfigPath: data.modsconfig_path ?? null,
        status: "ready",
      });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  loadUndo: async () => {
    set({ undoStatus: "loading", undoError: null });
    try {
      const data = await request<{ snapshots?: UndoSnapshot[] }>("get", "/api/undo");
      set({ undoSnapshots: data.snapshots ?? [], undoStatus: "ready" });
    } catch (error) {
      set({ undoStatus: "error", undoError: toMessage(error) });
    }
  },

  save: async (name) => {
    const result = await request<ProfileSaveResult>("post", "/api/profiles/save", {
      body: { name },
    });
    await useProfilesStore.getState().load();
    bump("profiles");
    return result;
  },

  restore: async (name) => {
    const result = await request<ProfileSaveResult>(
      "post",
      `/api/profiles/${encodeURIComponent(name)}/restore`,
    );
    bump("profiles");
    bump("mods");
    return result;
  },

  remove: async (name) => {
    await request("delete", `/api/profiles/${encodeURIComponent(name)}`);
    await useProfilesStore.getState().load();
    bump("profiles");
  },

  undo: async () => {
    const result = await request<UndoResult>("post", "/api/undo");
    await useProfilesStore.getState().loadUndo();
    bump("profiles");
    bump("mods");
    return result;
  },
}));
