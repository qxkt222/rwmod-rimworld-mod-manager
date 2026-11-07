/**
 * Tags slice — tag inventory, per-tag mod folders and the management
 * operations the legacy panel never exposed (create / rename / delete).
 *
 * The backend has no rename endpoint; rename is composed from add-new +
 * remove-old across every folder currently carrying the tag. That keeps the
 * UI honest without inventing a server-side contract.
 */
import { create } from "zustand";

import { apiRequest, toMessage } from "../api/client";
import type { components } from "../api/generated";
import { bump } from "./bus";
import type { LoadStatus } from "./config";

type TagCount = components["schemas"]["TagCount"];
type TagFoldersResponse = components["schemas"]["TagFoldersResponse"];

export interface TagDetail {
  tag: string;
  folders: string[];
}

interface TagsState {
  tags: TagCount[];
  detail: TagDetail | null;
  status: LoadStatus;
  detailStatus: LoadStatus;
  error: string | null;
  mutating: boolean;

  load: () => Promise<void>;
  loadDetail: (tag: string) => Promise<void>;
  clearDetail: () => void;
  addTag: (folder: string, tag: string) => Promise<void>;
  removeTag: (folder: string, tag: string) => Promise<void>;
  removeAllTags: (folder: string) => Promise<number>;
  renameTag: (oldTag: string, newTag: string) => Promise<number>;
}

export const useTagsStore = create<TagsState>()((set, get) => ({
  tags: [],
  detail: null,
  status: "idle",
  detailStatus: "idle",
  error: null,
  mutating: false,

  load: async () => {
    set({ status: "loading", error: null });
    try {
      const data = await apiRequest("get", "/api/tags");
      set({ tags: data.tags ?? [], status: "ready", error: null });
    } catch (error) {
      set({ status: "error", error: toMessage(error) });
      throw error;
    }
  },

  loadDetail: async (tag) => {
    set({ detailStatus: "loading", error: null });
    try {
      const data = (await apiRequest("get", `/api/tags/by-tag/${encodeURIComponent(tag)}`)) as
        TagFoldersResponse;
      set({
        detail: { tag: data.tag ?? tag, folders: data.folders ?? [] },
        detailStatus: "ready",
      });
    } catch (error) {
      set({ detailStatus: "error", error: toMessage(error) });
      throw error;
    }
  },

  clearDetail: () => set({ detail: null, detailStatus: "idle" }),

  addTag: async (folder, tag) => {
    set({ mutating: true, error: null });
    try {
      await apiRequest("post", `/api/tags/${encodeURIComponent(folder)}`, {
        body: { tag },
      });
      await get().load();
      bump("tags");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ mutating: false });
    }
  },

  removeTag: async (folder, tag) => {
    set({ mutating: true, error: null });
    try {
      await apiRequest("delete", `/api/tags/${encodeURIComponent(folder)}/${encodeURIComponent(tag)}`);
      await get().load();
      if (get().detail?.tag === tag) await get().loadDetail(tag);
      bump("tags");
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ mutating: false });
    }
  },

  removeAllTags: async (folder) => {
    set({ mutating: true, error: null });
    try {
      const result = (await apiRequest("delete", `/api/tags/${encodeURIComponent(folder)}`)) as {
        removed?: number;
      };
      await get().load();
      bump("tags");
      return result.removed ?? 0;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ mutating: false });
    }
  },

  renameTag: async (oldTag, newTag) => {
    const from = oldTag.trim();
    const to = newTag.trim();
    if (!from || !to) throw new Error("标签名不能为空");
    if (from === to) return 0;

    set({ mutating: true, error: null });
    try {
      const data = (await apiRequest("get", `/api/tags/by-tag/${encodeURIComponent(from)}`)) as
        TagFoldersResponse;
      const folders = data.folders ?? [];
      for (const folder of folders) {
        await apiRequest("post", `/api/tags/${encodeURIComponent(folder)}`, {
          body: { tag: to },
        });
        await apiRequest(
          "delete",
          `/api/tags/${encodeURIComponent(folder)}/${encodeURIComponent(from)}`,
        );
      }
      await get().load();
      await get().loadDetail(to);
      bump("tags");
      return folders.length;
    } catch (error) {
      set({ error: toMessage(error) });
      throw error;
    } finally {
      set({ mutating: false });
    }
  },
}));
