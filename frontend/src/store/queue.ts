/**
 * Queue slice — download queue plus the WebSocket connection state.
 *
 * Writers: the WS bridge (`setItems` / `setWsState`) and the future QueuePanel.
 * Subscribers: Sidebar badge, Dashboard, Search/Download/Collection/Import.
 */
import { create } from "zustand";

import { api, toMessage } from "../api/client";
import type { QueueItem } from "../api/types";
import type { WSConnectionState } from "../api/ws";
import { bump } from "./bus";
import { useConfigStore } from "./config";
import type { LoadStatus } from "./config";

const ACTIVE_STATUSES = new Set(["pending", "downloading"]);

/**
 * Pending + downloading rows. Derived with a selector instead of being
 * mirrored into the store (docs/ui-redesign.md §2.1 rule 4).
 */
export function selectPendingCount(state: { items: QueueItem[] }): number {
  return state.items.filter((item) => ACTIVE_STATUSES.has(item.status ?? "")).length;
}

interface QueueState {
  items: QueueItem[];
  paused: boolean;
  wsState: WSConnectionState;
  status: LoadStatus;
  error: string | null;

  refresh: () => Promise<void>;
  setItems: (items: QueueItem[]) => void;
  setWsState: (state: WSConnectionState) => void;
  add: (ids: string[], force?: boolean) => Promise<void>;
  start: () => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  clear: () => Promise<void>;
  remove: (id: string) => Promise<void>;
  retry: (ids?: string[]) => Promise<void>;
}

export const useQueueStore = create<QueueState>()((set, get) => ({
  items: [],
  paused: false,
  wsState: "connecting",
  status: "idle",
  error: null,

  refresh: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await api.getQueue();
      const items = data.items ?? [];
      set({ items, paused: Boolean(data.paused), status: "ready" });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  setItems: (items) => set({ items }),
  setWsState: (wsState) => set({ wsState }),

  add: async (ids, force = false) => {
    try {
      await api.addToQueue(ids, force);
      await get().refresh();
      bump("queue");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  start: async () => {
    try {
      await api.startQueue();
      await get().refresh();
      bump("queue");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  pause: async () => {
    try {
      await api.pauseQueue();
      set({ paused: true });
      bump("queue");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  resume: async () => {
    try {
      await api.resumeQueue();
      set({ paused: false });
      bump("queue");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  clear: async () => {
    try {
      await api.clearQueue();
      await get().refresh();
      bump("queue");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  remove: async (id) => {
    try {
      // Scope the delete to the active instance: the same workshop id may be
      // queued for two installations (routers/queue.py remove()).
      await api.removeQueueItem(id, useConfigStore.getState().activeInstanceId);
      await get().refresh();
      bump("queue");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },

  retry: async (ids = []) => {
    try {
      // Same scoping as remove(): the retry must target the active instance.
      await api.retryQueue(ids, useConfigStore.getState().activeInstanceId);
      await get().refresh();
      bump("queue");
      bump("dashboard");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    }
  },
}));
