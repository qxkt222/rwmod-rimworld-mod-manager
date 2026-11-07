/**
 * Updates slice — update check results and the auto-update runner.
 *
 * Writers: QueuePanel update section and the WS bridge (`update_notification`).
 * Subscribers: Dashboard pending count, Sidebar badge, Queue.
 *
 * The backend answers 409 (`ConflictError`) when a check is already running;
 * no 200 body ever carries `ok:false` (src/rwmod/routers/auto_update.py).
 */
import { create } from "zustand";

import { api, isConflict, toMessage } from "../api/client";
import type { AutoUpdateResult, UpdateItem } from "../api/types";
import { bump } from "./bus";

export type CheckStatus = "idle" | "checking" | "done" | "error";

/** User-facing text for the 409 "check already running" conflict. */
export const AUTO_UPDATE_CONFLICT_MESSAGE = "更新检查已在运行中";

interface UpdatesState {
  checkStatus: CheckStatus;
  outdated: UpdateItem[];
  autoUpdateRunning: boolean;
  error: string | null;

  check: () => Promise<void>;
  markOutdated: (items: UpdateItem[]) => void;
  clearOutdated: () => void;
  refreshStatus: () => Promise<void>;
  runAutoUpdate: () => Promise<AutoUpdateResult>;
}

export const useUpdatesStore = create<UpdatesState>()((set) => ({
  checkStatus: "idle",
  outdated: [],
  autoUpdateRunning: false,
  error: null,

  check: async () => {
    set({ checkStatus: "checking", error: null });
    try {
      const result = await api.checkUpdates();
      set({ outdated: result.updates ?? [], checkStatus: "done" });
      bump("updates");
      bump("dashboard");
    } catch (error) {
      set({ checkStatus: "error", error: toMessage(error) });
      throw error;
    }
  },

  markOutdated: (outdated) => set({ outdated, checkStatus: "done" }),
  clearOutdated: () => set({ outdated: [], checkStatus: "idle" }),

  refreshStatus: async () => {
    try {
      const status = await api.getAutoUpdateStatus();
      set({ autoUpdateRunning: status.running });
    } catch {
      /* status probe is best effort — the runner reports its own errors */
    }
  },

  runAutoUpdate: async () => {
    set({ autoUpdateRunning: true, error: null });
    try {
      const result = await api.runAutoUpdate();
      // run_check() awaits the whole check, so a 200 means it has finished.
      set({ autoUpdateRunning: false });
      bump("updates");
      bump("dashboard");
      return result;
    } catch (error) {
      // 409 means another runner owns the check — it is active, not failed.
      set({ autoUpdateRunning: isConflict(error), error: toMessage(error) });
      throw error;
    }
  },
}));
