/**
 * RimSort slice (FE-3 wave 2) — ModsConfig generation, uploaded-file compare
 * and load-order analysis. All three are one-shot queries owned by the panel;
 * the slice exists so the panel never talks to `fetch` directly and so results
 * survive a tab switch (legacy behaviour kept the <pre> contents alive).
 */
import { create } from "zustand";

import { request, toMessage } from "../api/client";
import type { LoadStatus } from "./config";

export interface MissingModDetail {
  package_id?: string;
  workshop_id?: string;
  resolved?: boolean;
}

export interface RimsortCompareResult {
  /** Legacy parse-failure field the backend still returns for bad XML. */
  error?: string;
  total_in_config?: number;
  installed?: string[];
  missing?: string[];
  extra?: string[];
  missing_details?: MissingModDetail[];
}

export interface LoadOrderIssue {
  severity: string;
  message: string;
}

export interface LoadOrderResult {
  total_mods?: number;
  issues?: LoadOrderIssue[];
  load_order?: string[];
  ok?: boolean;
}

interface RimsortState {
  xml: string;
  generateStatus: LoadStatus;
  compare: RimsortCompareResult | null;
  compareStatus: LoadStatus;
  order: LoadOrderResult | null;
  orderStatus: LoadStatus;
  error: string | null;

  generate: () => Promise<string>;
  compareFile: (file: File) => Promise<void>;
  checkOrder: () => Promise<void>;
  clearOutput: () => void;
}

export const useRimsortStore = create<RimsortState>()((set) => ({
  xml: "",
  generateStatus: "idle",
  compare: null,
  compareStatus: "idle",
  order: null,
  orderStatus: "idle",
  error: null,

  generate: async () => {
    set({ generateStatus: "loading", error: null });
    try {
      const data = await request<{ modsconfig_xml?: string }>(
        "post",
        "/api/rimsort/generate",
      );
      const xml = data.modsconfig_xml ?? "";
      set({ xml, generateStatus: xml ? "ready" : "error", error: xml ? null : "未返回 ModsConfig 内容" });
      return xml;
    } catch (error) {
      set({ generateStatus: "error", error: toMessage(error) });
      throw error;
    }
  },

  compareFile: async (file) => {
    set({ compareStatus: "loading", error: null });
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await request<RimsortCompareResult>("post", "/api/rimsort/compare-file", {
        form,
      });
      set({ compare: data, compareStatus: "ready" });
    } catch (error) {
      set({ compareStatus: "error", error: toMessage(error) });
      throw error;
    }
  },

  checkOrder: async () => {
    set({ orderStatus: "loading", error: null });
    try {
      const data = await request<LoadOrderResult>("get", "/api/rimsort/check-order");
      set({ order: data, orderStatus: "ready" });
    } catch (error) {
      set({ orderStatus: "error", error: toMessage(error) });
      throw error;
    }
  },

  clearOutput: () => set({ xml: "" }),
}));
