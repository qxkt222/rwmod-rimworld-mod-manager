/**
 * WebSocket bridge: one socket for the whole app, feeding the queue/updates
 * slices (docs/ui-redesign.md §4.2).
 *
 * The legacy `src/ws.ts` stays untouched for the Vanilla entry. This module
 * owns the React lifetime: reconnect backoff, 5s REST polling fallback and the
 * synthetic `connected` / `disconnected` events that let the UI show whether
 * the badge numbers are live.
 */
import { bump } from "../store/bus";
import { useQueueStore } from "../store/queue";
import { notify } from "../store/ui";
import { useUpdatesStore } from "../store/updates";
import { api } from "./client";
import type { QueueItem, UpdateItem } from "./types";

export type WSConnectionState = "connected" | "disconnected" | "connecting";

export interface WSMessage {
  type: string;
  /** Set on the locally synthesised connection-state events. */
  state?: WSConnectionState;
  items?: QueueItem[];
  count?: number;
  updates?: UpdateItem[];
  [key: string]: unknown;
}

const MAX_RECONNECT = 5;
const POLL_INTERVAL_MS = 5_000;

let socket: WebSocket | null = null;
let reconnectCount = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let started = false;

/** Apply one WS frame to the stores. Exported for unit tests. */
export function dispatchWSMessage(msg: WSMessage): void {
  const queue = useQueueStore.getState();
  const updates = useUpdatesStore.getState();

  switch (msg.type) {
    case "connected":
      queue.setWsState("connected");
      stopPolling();
      // Align state that changed while the socket was down.
      void queue.refresh();
      void updates.refreshStatus();
      break;
    case "disconnected":
      queue.setWsState("disconnected");
      break;
    case "queue_update":
      queue.setItems(msg.items ?? []);
      break;
    case "update_notification": {
      const items = msg.updates ?? [];
      updates.markOutdated(items);
      const count = msg.count ?? items.length;
      if (count > 0) notify.info(`发现 ${count} 个更新`);
      bump("updates");
      bump("dashboard");
      break;
    }
    default:
      break;
  }
}

/** Open (or reuse) the singleton socket. */
export function connectWS(): WebSocket | null {
  if (socket && socket.readyState !== WebSocket.CLOSED) return socket;

  if (typeof WebSocket === "undefined") {
    dispatchWSMessage({ type: "disconnected" });
    startPolling();
    return null;
  }

  useQueueStore.getState().setWsState("connecting");

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const url = `${protocol}://${location.host}/ws`;

  let ws: WebSocket;
  try {
    ws = new WebSocket(url);
  } catch {
    dispatchWSMessage({ type: "disconnected" });
    startPolling();
    return null;
  }
  socket = ws;

  ws.onopen = () => {
    reconnectCount = 0;
    dispatchWSMessage({ type: "connected", state: "connected" });
  };

  ws.onmessage = (event: MessageEvent<string>) => {
    try {
      dispatchWSMessage(JSON.parse(event.data) as WSMessage);
    } catch {
      /* ignore malformed frames */
    }
  };

  ws.onclose = () => {
    socket = null;
    reconnectCount += 1;
    dispatchWSMessage({ type: "disconnected", state: "disconnected" });
    if (reconnectCount <= MAX_RECONNECT) {
      const delay = Math.min(2_000 * reconnectCount, 15_000);
      reconnectTimer = setTimeout(() => connectWS(), delay);
    } else {
      startPolling();
    }
  };

  ws.onerror = () => ws.close();
  return ws;
}

/** Start the bridge once; safe to call from bootstrap only. */
export function startWsBridge(): void {
  if (started) return;
  started = true;
  connectWS();
}

/** Tear the bridge down (tests / hot reload). */
export function stopWsBridge(): void {
  started = false;
  stopPolling();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (socket) {
    const current = socket;
    socket = null;
    current.onclose = null;
    current.close();
  }
  reconnectCount = 0;
}

/** REST fallback: poll /api/queue every 5s while the socket is unavailable. */
function startPolling(): void {
  if (pollTimer) return;
  // Assign before the first tick — tick() may call connectWS(), whose failure
  // path re-enters startPolling(). Without the guard set first this recurses
  // and leaks one interval per level (the legacy module hit 2600+).
  pollTimer = setInterval(() => void tick(), POLL_INTERVAL_MS);
  void tick();
}

async function tick(): Promise<void> {
  try {
    const recovered = connectWS();
    if (recovered && recovered.readyState === WebSocket.OPEN) {
      stopPolling();
      return;
    }
    const data = await api.getQueue();
    dispatchWSMessage({ type: "queue_update", items: data.items ?? [] });
  } catch {
    /* network hiccup — keep polling */
  }
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
