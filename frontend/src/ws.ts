/**
 * WebSocket client for real-time download progress.
 * Falls back to SSE via api.downloadStream if WS unavailable.
 *
 * Auth: the backend /ws endpoint validates a JWT presented as the
 * ``rwmod.<token>`` subprotocol (Sec-WebSocket-Protocol header) — carrying it
 * in the URL (?token=) would leak the token into access logs / browser
 * history. The legacy query-param form is still accepted by the server.
 * If the token is missing/expired the connection is rejected — after
 * MAX_RECONNECT attempts we fall back to REST polling of /api/queue (every
 * 5s), and any 401 on those fetches triggers the unified re-login flow
 * (patchFetchWithAuth / fetchJSON). Once the socket reconnects (e.g. after
 * re-login), polling stops automatically.
 */
import { getToken } from "./auth";

const WS_PROTOCOL_PREFIX = "rwmod.";

export interface WSMessage {
  type: string;
  mod_id?: string;
  msg?: string;
  line?: string;
  total?: number;
  ok?: number;
  fail?: number;
  items?: { id: string; name: string; status: string; progress: number; msg: string }[];
}

type WSCallback = (msg: WSMessage) => void;

let ws: WebSocket | null = null;
let listeners: WSCallback[] = [];
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let reconnectCount = 0;
let pollTimer: ReturnType<typeof setInterval> | null = null;
let manualClose = false;
const MAX_RECONNECT = 5;
const POLL_INTERVAL_MS = 5000;

export function connectWS(onMessage: WSCallback): WebSocket | null {
  // Reuse an existing socket (OPEN or still CONNECTING) — avoids duplicate
  // sockets when the polling fallback probes for a reconnect.
  if (ws && ws.readyState !== WebSocket.CLOSED) return ws;

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const url = new URL(`${protocol}://${location.host}/ws`);
  // Present the token as a WebSocket subprotocol instead of a query param —
  // the server echoes it back on accept, and the browser refuses the
  // handshake if the server doesn't (which the backend does).
  const token = getToken();
  const protocols = token ? [WS_PROTOCOL_PREFIX + token] : [];
  try {
    ws = new WebSocket(url.toString(), protocols);
  } catch {
    console.log("[WS] WebSocket not available, using REST fallback");
    return null;
  }
  manualClose = false;

  ws.onopen = () => {
    console.log("[WS] connected");
    reconnectCount = 0;
    stopPolling(); // WS 恢复后切回实时通道
  };

  ws.onmessage = (e) => {
    try {
      const msg: WSMessage = JSON.parse(e.data);
      onMessage(msg);
      for (const fn of listeners) fn(msg);
    } catch { /* ignore */ }
  };

  ws.onclose = () => {
    if (manualClose) return;
    console.log("[WS] disconnected");
    ws = null;
    reconnectCount += 1;
    if (reconnectCount <= MAX_RECONNECT) {
      const delay = Math.min(2000 * reconnectCount, 15000);
      reconnectTimer = setTimeout(() => connectWS(onMessage), delay);
    } else {
      console.log("[WS] max retries reached, using REST fallback");
      startPolling(onMessage);
    }
  };

  ws.onerror = () => ws?.close();

  return ws;
}

/** REST 轮询兜底：每 5s 拉取 /api/queue，同时探测 WS 是否恢复。 */
function startPolling(onMessage: WSCallback): void {
  if (pollTimer) return;
  console.log("[WS] REST 轮询兜底已启动（每 5s 查询 /api/queue）");
  const tick = async () => {
    // 尝试恢复 WS：成功连接后 onopen 会停止轮询
    const sock = connectWS(onMessage);
    if (sock && sock.readyState === WebSocket.OPEN) {
      stopPolling();
      return;
    }
    try {
      const resp = await fetch("/api/queue");
      if (!resp.ok) return;
      const data = await resp.json();
      const msg: WSMessage = { type: "queue_update", items: data.items };
      onMessage(msg);
      for (const fn of listeners) fn(msg);
    } catch { /* 网络错误 — 继续轮询 */ }
  };
  tick();
  pollTimer = setInterval(tick, POLL_INTERVAL_MS);
}

function stopPolling(): void {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

export function addWSListener(fn: WSCallback) {
  listeners.push(fn);
}

export function removeWSListener(fn: WSCallback) {
  listeners = listeners.filter((f) => f !== fn);
}

export function disconnectWS() {
  if (reconnectTimer) clearTimeout(reconnectTimer);
  stopPolling();
  manualClose = true;
  listeners = [];
  ws?.close();
  ws = null;
}
