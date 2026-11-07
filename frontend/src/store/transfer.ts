/**
 * Transfer slice — SSE log buffers for download / collection / import.
 *
 * The stream lives in the store, not the panel: switching away keeps the log
 * and the connection alive, exactly like the legacy panels' module-level state.
 * Panels only subscribe; `clear()` or a terminal `done` frame closes the stream.
 */
import { create } from "zustand";

import { openSSE, type SseHandle } from "../api/sse";

export type ChannelName = "download" | "collection" | "import";

export interface TransferChannel {
  lines: string[];
  /** 0..100 — last reported percentage. */
  progress: number;
  streaming: boolean;
  error: string | null;
}

interface StreamFrame {
  event?: string;
  msg?: string;
  line?: string;
  percent?: number;
}

const MAX_LINES = 2_000;

function emptyChannel(): TransferChannel {
  return { lines: [], progress: 0, streaming: false, error: null };
}

function emptyChannels(): Record<ChannelName, TransferChannel> {
  return {
    download: emptyChannel(),
    collection: emptyChannel(),
    import: emptyChannel(),
  };
}

interface TransferState {
  channels: Record<ChannelName, TransferChannel>;

  start: (channel: ChannelName) => void;
  append: (channel: ChannelName, line: string) => void;
  setProgress: (channel: ChannelName, progress: number) => void;
  finish: (channel: ChannelName) => void;
  fail: (channel: ChannelName, message: string) => void;
  clear: (channel: ChannelName) => void;
  startStream: (channel: ChannelName, url: string) => void;
  closeStream: (channel: ChannelName) => void;
}

/** Live handles are module state, not React state — they are not renderable. */
const handles: Partial<Record<ChannelName, SseHandle>> = {};

function patch(
  state: TransferState,
  channel: ChannelName,
  update: Partial<TransferChannel>,
): Pick<TransferState, "channels"> {
  return {
    channels: {
      ...state.channels,
      [channel]: { ...state.channels[channel], ...update },
    },
  };
}

export const useTransferStore = create<TransferState>()((set, get) => ({
  channels: emptyChannels(),

  start: (channel) =>
    set((state) => patch(state, channel, { ...emptyChannel(), streaming: true })),

  append: (channel, line) =>
    set((state) => {
      const lines = [...state.channels[channel].lines, line];
      return patch(state, channel, {
        lines: lines.length > MAX_LINES ? lines.slice(-MAX_LINES) : lines,
      });
    }),

  setProgress: (channel, progress) => set((state) => patch(state, channel, { progress })),

  finish: (channel) => {
    handles[channel]?.close();
    delete handles[channel];
    set((state) => patch(state, channel, { streaming: false }));
  },

  fail: (channel, message) => {
    handles[channel]?.close();
    delete handles[channel];
    set((state) => patch(state, channel, { streaming: false, error: message }));
  },

  clear: (channel) => {
    handles[channel]?.close();
    delete handles[channel];
    set((state) => patch(state, channel, emptyChannel()));
  },

  startStream: (channel, url) => {
    get().clear(channel);
    get().start(channel);
    const handle = openSSE(url, {
      onMessage: (data) => {
        const frame = (data ?? {}) as StreamFrame;
        const text = frame.line ?? frame.msg;
        if (text) get().append(channel, text);

        if (typeof frame.percent === "number") {
          get().setProgress(channel, frame.percent);
        }
        if (frame.event === "done") {
          get().finish(channel);
        } else if (frame.event === "fail") {
          get().fail(channel, frame.msg ?? "传输失败");
        }
      },
      onError: (error) => {
        get().fail(channel, error instanceof Error ? error.message : "连接中断");
      },
    });
    handles[channel] = handle;
  },

  closeStream: (channel) => {
    handles[channel]?.close();
    delete handles[channel];
    set((state) => patch(state, channel, { streaming: false }));
  },
}));
