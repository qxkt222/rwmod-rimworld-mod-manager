/**
 * Config slice — active instance, SteamCMD probe and Steam API online state.
 *
 * Owners: bootstrap (initial load) and the future ConfigPanel (mutations).
 * Subscribers: Sidebar, Topbar, Dashboard and every panel that needs paths.
 */
import { create } from "zustand";

import { api, toMessage } from "../api/client";
import type { ConfigData, InstanceData, SteamcmdCheck } from "../api/types";
import { bump } from "./bus";
import { useModsStore } from "./mods";

export type LoadStatus = "idle" | "loading" | "ready" | "error";

interface ConfigState {
  config: ConfigData | null;
  instances: InstanceData[];
  activeInstanceId: string | null;
  /** null = not probed yet; false = Steam API unreachable. */
  steamOnline: boolean | null;
  steamcmdCheck: SteamcmdCheck | null;
  status: LoadStatus;
  error: string | null;

  load: () => Promise<void>;
  refreshStatus: () => Promise<void>;
  checkSteamcmd: () => Promise<void>;
  save: (patch: Record<string, unknown>) => Promise<void>;
  createInstance: (payload: Record<string, unknown>) => Promise<void>;
  activateInstance: (id: string) => Promise<void>;
  deleteInstance: (id: string) => Promise<void>;
}

/**
 * Refresh the mods slice after the active instance (or its mods_dir) changes,
 * so the sidebar badge never shows the previous instance's count.
 * Failures stay inside the mods slice — the config mutation itself succeeded.
 */
async function reloadMods(): Promise<void> {
  await useModsStore
    .getState()
    .load()
    .catch(() => {
      /* badge keeps the old count until the next successful load */
    });
}

export const useConfigStore = create<ConfigState>()((set, get) => ({
  config: null,
  instances: [],
  activeInstanceId: null,
  steamOnline: null,
  steamcmdCheck: null,
  status: "idle",
  error: null,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const config = await api.getConfig();
      set({
        config,
        instances: config.instances ?? [],
        activeInstanceId: config.active_instance ?? null,
        status: "ready",
      });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  refreshStatus: async () => {
    try {
      const status = await api.getStatus();
      set({ steamOnline: status.online });
    } catch {
      set({ steamOnline: false });
    }
  },

  checkSteamcmd: async () => {
    try {
      const result = await api.checkSteamcmd();
      set({ steamcmdCheck: result });
    } catch (error) {
      set({ steamcmdCheck: { ok: false, msg: toMessage(error) } });
    }
  },

  save: async (patch) => {
    try {
      await api.saveConfig(patch);
      await get().load();
      bump("config");
      if ("mods_dir" in patch) {
        // The installed-mod set belongs to the mods directory — the sidebar
        // badge must drop the old instance's count (REV-009).
        await reloadMods();
        bump("mods");
      }
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  createInstance: async (payload) => {
    try {
      const result = await api.createInstance(payload);
      set((state) => ({ instances: [...state.instances, result.instance] }));
      bump("config");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  activateInstance: async (id) => {
    try {
      const result = await api.activateInstance(id);
      set({ activeInstanceId: result.active_instance });
      await get().load();
      // Every instance has its own mods directory: reload the badge count
      // immediately instead of waiting for the Mods panel to mount.
      await reloadMods();
      bump("config");
      bump("mods");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  deleteInstance: async (id) => {
    try {
      await api.deleteInstance(id);
      set((state) => ({ instances: state.instances.filter((item) => item.id !== id) }));
      bump("config");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },
}));
