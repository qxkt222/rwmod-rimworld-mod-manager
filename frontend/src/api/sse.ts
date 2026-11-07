/**
 * Server-Sent Events helper for the streaming panels (download / collection /
 * import). The backend emits unnamed `data: {json}` frames whose `event` field
 * carries the semantic type, so the default `message` channel is all we need.
 *
 * Two transports:
 *  - `EventSource` when the runtime provides it (browsers);
 *  - a `fetch` + `ReadableStream` reader as the fallback (happy-dom / older
 *    WebViews). The fallback mirrors the legacy implementation in `api.ts` but
 *    is not used by the Vanilla entry — old code stays untouched.
 */
export interface SseHandlers {
  /** One decoded JSON frame. Malformed frames are skipped. */
  onMessage?: (data: unknown) => void;
  onOpen?: () => void;
  onError?: (error: unknown) => void;
}

export interface SseHandle {
  /** Close the stream. Idempotent. */
  close(): void;
  /** True until `close()` or a terminal transport error. */
  readonly closed: boolean;
}

function absolute(url: string): string {
  if (/^https?:\/\//i.test(url)) return url;
  const origin = typeof window !== "undefined" ? window.location.origin : "http://localhost";
  return new URL(url, origin).toString();
}

/** Open an SSE endpoint and decode each frame as JSON. */
export function openSSE(url: string, handlers: SseHandlers = {}): SseHandle {
  if (typeof EventSource !== "undefined") {
    return openEventSource(url, handlers);
  }
  return openFetchStream(url, handlers);
}

function openEventSource(url: string, handlers: SseHandlers): SseHandle {
  const source = new EventSource(url);
  let closed = false;

  source.onopen = () => handlers.onOpen?.();
  source.onmessage = (event: MessageEvent<string>) => {
    try {
      handlers.onMessage?.(JSON.parse(event.data));
    } catch {
      /* ignore malformed frames — one bad line must not kill the stream */
    }
  };
  source.onerror = (event) => {
    // EventSource reconnects on its own; surface the error but stay open.
    if (!closed) handlers.onError?.(event);
  };

  return {
    get closed() {
      return closed;
    },
    close() {
      if (closed) return;
      closed = true;
      source.close();
    },
  };
}

function openFetchStream(url: string, handlers: SseHandlers): SseHandle {
  const controller = new AbortController();
  let closed = false;

  const finish = () => {
    if (closed) return;
    closed = true;
    controller.abort();
  };

  void (async () => {
    try {
      const response = await fetch(absolute(url), { signal: controller.signal });
      if (!response.ok) {
        handlers.onError?.(new Error(`HTTP ${response.status}`));
        finish();
        return;
      }
      handlers.onOpen?.();

      const reader = response.body?.getReader();
      if (!reader) {
        finish();
        return;
      }

      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          try {
            handlers.onMessage?.(JSON.parse(line.slice(6)));
          } catch {
            /* ignore malformed frames */
          }
        }
      }
      finish();
    } catch (error) {
      if (controller.signal.aborted) return;
      handlers.onError?.(error);
      finish();
    }
  })();

  return {
    get closed() {
      return closed;
    },
    close: finish,
  };
}
