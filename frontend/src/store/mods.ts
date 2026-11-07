/**
 * Mods slice — installed mods, selection and health/compat badges.
 *
 * Owner: the future ModsPanel (plus bus invalidation after downloads/imports).
 * Subscribers: Dashboard counts, Sidebar badge, Rimsort/Profiles/Tags.
 */
import { create } from "zustand";

import { api, toMessage } from "../api/client";
import type { ModCompatibility, ModEntry } from "../api/types";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

interface ModsState {
  items: ModEntry[];
  onlyActive: boolean;
  selectedIds: string[];
  health: Record<string, string>;
  compat: ModCompatibility | null;
  status: LoadStatus;
  error: string | null;

  load: () => Promise<void>;
  setOnlyActive: (onlyActive: boolean) => void;
  toggleSelected: (packageId: string) => void;
  selectAll: () => void;
  clearSelection: () => void;
  setActive: (packageIds: string[], active: boolean) => Promise<void>;
  loadBadges: () => Promise<void>;
}

export const useModsStore = create<ModsState>()((set, get) => ({
  items: [],
  onlyActive: false,
  selectedIds: [],
  health: {},
  compat: null,
  status: "idle",
  error: null,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const items = await api.listMods();
      set({ items, status: "ready", selectedIds: [] });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  setOnlyActive: (onlyActive) => set({ onlyActive }),
  toggleSelected: (packageId) =>
    set((state) => ({
      selectedIds: state.selectedIds.includes(packageId)
        ? state.selectedIds.filter((id) => id !== packageId)
        : [...state.selectedIds, packageId],
    })),
  selectAll: () =>
    set((state) => {
      const visible = state.onlyActive
        ? state.items.filter((item) => item.active)
        : state.items;
      return { selectedIds: visible.map((item) => item.package_id) };
    }),
  clearSelection: () => set({ selectedIds: [] }),

  setActive: async (packageIds, active) => {
    if (packageIds.length === 0) return;
    try {
      await api.setModsActive(active ? packageIds : [], active ? [] : packageIds);
      await get().load();
      bump("mods");
      bump("rimsort");
      bump("profiles");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  loadBadges: async () => {
    const [health, compat] = await Promise.allSettled([
      api.getModHealth(),
      api.getModCompatibility(),
    ]);
    if (health.status === "fulfilled") {
      const map: Record<string, string> = {};
      for (const entry of health.value.mods ?? []) map[entry.folder] = entry.status;
      set({ health: map });
    }
    if (compat.status === "fulfilled") {
      set({ compat: compat.value });
    }
  },
}));
