/**
 * History slice — download history rows plus aggregate success/failure stats.
 *
 * Owner: HistoryPanel. Auto-update controls intentionally stay in the shared
 * `updates` slice (FE-1); this slice only carries the history list and the
 * clear mutation, matching the legacy `panels/history.ts` split.
 */
import { create } from "zustand";

import { apiRequest, toMessage } from "../api/client";
import type { HistoryItem } from "../api/types";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

export interface HistoryStats {
  total: number;
  success: number;
  failed: number;
}

interface HistoryState {
  items: HistoryItem[];
  stats: HistoryStats;
  status: LoadStatus;
  error: string | null;

  load: () => Promise<void>;
  clear: () => Promise<void>;
}

const EMPTY_STATS: HistoryStats = { total: 0, success: 0, failed: 0 };

export const useHistoryStore = create<HistoryState>()((set, get) => ({
  items: [],
  stats: EMPTY_STATS,
  status: "idle",
  error: null,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const [history, stats] = await Promise.all([
        apiRequest("get", "/api/history", { query: { limit: 50 } }),
        apiRequest("get", "/api/history/stats"),
      ]);
      const items = (history as { items?: HistoryItem[] } | undefined)?.items ?? [];
      const summary = (stats ?? EMPTY_STATS) as HistoryStats;
      set({ items, stats: summary, status: "ready", error: null });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  clear: async () => {
    try {
      await apiRequest("post", "/api/history/clear");
      await get().load();
      bump("history");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },
}));
