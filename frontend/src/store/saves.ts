/**
 * Saves slice — save-file scan results and .rws upload analysis.
 *
 * Owner: SavesPanel. Scanning reads the configured Saves directory; the
 * upload path parses XML only (no downloads).
 */
import { create } from "zustand";

import { apiRequest, toMessage } from "../api/client";
import type { components } from "../api/generated";
import type { LoadStatus } from "./config";

type SaveSummary = components["schemas"]["SaveSummary"];
type SaveAnalyzeResponse = components["schemas"]["SaveAnalyzeResponse"];

interface SavesState {
  saves: SaveSummary[];
  status: LoadStatus;
  error: string | null;
  analyzing: boolean;

  scan: () => Promise<void>;
  analyze: (file: File) => Promise<SaveAnalyzeResponse>;
}

export const useSavesStore = create<SavesState>()((set, get) => ({
  saves: [],
  status: "idle",
  error: null,
  analyzing: false,

  scan: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await apiRequest("get", "/api/saves");
      // NOTE: never bump("saves") here — the panel subscribes to that topic,
      // so bumping from inside scan() would retrigger itself forever.
      set({ saves: data.saves ?? [], status: "ready", error: null });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  analyze: async (file) => {
    set({ analyzing: true, error: null });
    try {
      const form = new FormData();
      form.append("file", file);
      const result = (await apiRequest("post", "/api/saves/analyze", {
        form,
      })) as SaveAnalyzeResponse;
      await get().scan();
      return result;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ analyzing: false });
    }
  },
}));
