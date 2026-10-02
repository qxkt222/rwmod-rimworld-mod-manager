/**
 * WebSocket client for real-time download progress.
 * Falls back to REST polling (api.getQueue) when the socket is unavailable.
 */
import { api, type QueueItem } from "./api";

/** Connection-state events this module emits locally (never sent by the server). */
export type WSConnectionState = "connected" | "disconnected";

export interface WSMessage {
  type: string;
  /** Set on the local "connected"/"disconnected" events emitted by this module. */
  state?: WSConnectionState;
  mod_id?: string;
  msg?: string;
  line?: string;
  total?: number;
  ok?: number;
  fail?: number;
  items?: QueueItem[];
}

type WSCallback = (msg: WSMessage) => void;

let ws: WebSocket | null = null;
const listeners: WSCallback[] = [];
let reconnectCount = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
const MAX_RECONNECT = 5;
const POLL_INTERVAL_MS = 5000;

/**
 * Deliver a message to every subscriber.
 * A throwing listener used to abort the whole fan-out loop — one bad handler
 * silently cut the socket pipe for everyone else.
 */
function emit(msg: WSMessage): void {
  for (const fn of listeners) {
    try {
      fn(msg);
    } catch (e) {
      console.error("[WS] listener error", e);
    }
  }
}

/** Subscribe to WS messages, including the local connected/disconnected events. */
export function addWSListener(fn: WSCallback): void {
  listeners.push(fn);
}

/**
 * Ensure the socket exists, and optionally subscribe a handler for this and
 * all future (re)connections.
 *
 * Previously a second connectWS() call with a new handler returned the
 * existing socket and silently dropped that handler; reconnects also only
 * replayed the first one. Handlers now all live in the shared listener list,
 * so late registrations work no matter who opened the socket.
 */
export function connectWS(onMessage?: WSCallback): WebSocket | null {
  if (onMessage) addWSListener(onMessage);

  // Reuse an existing socket (OPEN or still CONNECTING) — avoids duplicate
  // sockets when the polling fallback probes for a reconnect.
  if (ws && ws.readyState !== WebSocket.CLOSED) return ws;

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${location.host}/ws`);
  try {
    ws = new WebSocket(url.toString());
  } catch {
    console.log("[WS] WebSocket not available, using REST fallback");
    emit({ type: "disconnected", state: "disconnected" });
    startPolling();
    return null;
  }

  ws.onopen = () => {
    console.log("[WS] connected");
    reconnectCount = 0;
    stopPolling(); // WS 恢复后切回实时通道
    emit({ type: "connected", state: "connected" });
  };

  ws.onmessage = (e) => {
    try {
      emit(JSON.parse(e.data));
    } catch { /* ignore bad JSON */ }
  };

  ws.onclose = () => {
    console.log("[WS] disconnected");
    ws = null;
    reconnectCount += 1;
    emit({ type: "disconnected", state: "disconnected" });
    if (reconnectCount <= MAX_RECONNECT) {
      const delay = Math.min(2000 * reconnectCount, 15000);
      setTimeout(() => connectWS(), delay);
    } else {
      console.log("[WS] max retries reached, using REST fallback");
      startPolling();
    }
  };

  ws.onerror = () => ws?.close();

  return ws;
}

/** REST 轮询兜底：每 5s 拉取 /api/queue，同时探测 WS 是否恢复。 */
function startPolling(): void {
  if (pollTimer) return;
  console.log("[WS] REST 轮询兜底已启动（每 5s 查询 /api/queue）");
  const tick = async () => {
    try {
      // 尝试恢复 WS：成功连接后 onopen 会停止轮询
      const sock = connectWS();
      if (sock && sock.readyState === WebSocket.OPEN) {
        stopPolling();
        return;
      }
      const data = await api.getQueue();
      emit({ type: "queue_update", items: data.items });
    } catch { /* 网络错误/构造失败 — 继续轮询 */ }
  };
  // 必须先占位 pollTimer 再跑首次 tick：tick 会调用 connectWS()，当 WebSocket
  // 构造失败时 connectWS 的 catch 又会进入 startPolling()。若此时 pollTimer
  // 尚未赋值，重入保护失效 → tick 同步递归到栈溢出，且每层回退时各创建一个
  // setInterval（实测单次调用泄漏 2600+ 个定时器）。先赋值即杜绝该路径。
  pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  void tick();
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}
