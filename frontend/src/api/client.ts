/**
 * Typed HTTP client for the React app — the single REST exit for every slice.
 *
 * Design rules (docs/ui-redesign.md §2.3):
 *  - `request()` is the only place that calls `fetch`;
 *  - paths are checked against the generated OpenAPI map (`generated.ts`);
 *  - every non-2xx response becomes an `ApiError` carrying the backend's
 *    uniform failure body `{ error, detail }` (src/rwmod/server.py handlers).
 */
import type { paths } from "./generated";
import type {
  AppStatus,
  AutoUpdateResult,
  AutoUpdateStatus,
  CheckUpdatesResult,
  ConfigData,
  DashboardData,
  InstanceData,
  ModCompatibility,
  ModEntry,
  ModHealthResult,
  OkResponse,
  QueueItem,
  QueueState,
  SteamcmdCheck,
  UpdateItem,
} from "./types";

// Generated `paths` keys already start with `/api`, so paths are used verbatim.

export type HttpMethod = "get" | "post" | "put" | "patch" | "delete";

export type ApiPath = keyof paths;

/** HTTP verbs actually present on a generated path. */
export type MethodOf<P extends ApiPath> = Extract<keyof paths[P], HttpMethod>;

/** JSON body of the 200 response declared for `path` + `method`. */
export type OkBody<P extends ApiPath, M extends MethodOf<P>> = paths[P][M] extends {
  responses: { 200: { content: { "application/json": infer R } } };
}
  ? R
  : unknown;

export type QueryValue = string | number | boolean | null | undefined;
export type QueryParams = Record<string, QueryValue>;

export interface RequestOptions {
  /** JSON body (ignored when `form` is set). */
  body?: unknown;
  /** multipart/form-data body. */
  form?: FormData;
  query?: QueryParams;
  signal?: AbortSignal;
  headers?: Record<string, string>;
}

/** Human-readable message for any thrown value (stores keep it in `error`). */
export function toMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * True when the failure is an HTTP 409 conflict (e.g. an update check is
 * already running). Callers treat it as informational, not as a failure.
 */
export function isConflict(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 409 || error.code === "ConflictError");
}

/** Uniform API failure — components branch on `status`/`code`, toast `detail`. */
export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly detail: string;

  constructor(detail: string, status: number, code?: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function buildUrl(path: string, query?: QueryParams): string {
  const origin = typeof window !== "undefined" ? window.location.origin : "http://localhost";
  const url = new URL(path, origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* non-JSON error body — fall through to the status text */
  }
  const record = (body ?? {}) as { error?: unknown; detail?: unknown };
  const detail =
    typeof record.detail === "string" && record.detail.length > 0
      ? record.detail
      : response.statusText || `HTTP ${response.status}`;
  const code = typeof record.error === "string" ? record.error : undefined;
  return new ApiError(detail, response.status, code);
}

/** Core request. Returns the parsed JSON body (or `undefined` for 204/empty). */
export async function request<T>(
  method: HttpMethod,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const init: RequestInit = { method: method.toUpperCase(), signal: options.signal };
  const headers: Record<string, string> = { ...(options.headers ?? {}) };

  if (options.form) {
    init.body = options.form;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  init.headers = headers;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), init);
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      error instanceof Error ? error.message : "网络请求失败",
      0,
      "NetworkError",
    );
  }

  if (!response.ok) throw await toApiError(response);

  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (text.length === 0) return undefined as T;
  return JSON.parse(text) as T;
}

/** OpenAPI-typed request helper: response type follows the generated schema. */
export function apiRequest<P extends ApiPath, M extends MethodOf<P>>(
  method: M,
  path: P,
  options?: RequestOptions,
): Promise<OkBody<P, M>>;
/** Dynamic path (e.g. `/api/queue/${id}`) — caller narrows the result. */
export function apiRequest(
  method: HttpMethod,
  path: string,
  options?: RequestOptions,
): Promise<unknown>;
export function apiRequest(
  method: HttpMethod,
  path: string,
  options?: RequestOptions,
): Promise<unknown> {
  return request<unknown>(method, path, options);
}

/**
 * Narrow facade for the endpoints used by the shared layer (wave 2) and the
 * dashboard pilot. FE-2/FE-3 extend this object rather than calling
 * `fetch`/`request` from components.
 */
export const api = {
  // dashboard / status
  getDashboard: () => apiRequest("get", "/api/dashboard") as Promise<DashboardData>,
  getStatus: () => apiRequest("get", "/api/status") as Promise<AppStatus>,
  checkSteamcmd: () => apiRequest("get", "/api/steamcmd/check") as Promise<SteamcmdCheck>,

  // config
  getConfig: () => apiRequest("get", "/api/config") as Promise<ConfigData>,
  saveConfig: (patch: Record<string, unknown>) =>
    apiRequest("post", "/api/config", { body: patch }) as Promise<OkResponse>,
  createInstance: (payload: Record<string, unknown>) =>
    apiRequest("post", "/api/config/instances", { body: payload }) as Promise<{
      ok: boolean;
      instance: InstanceData;
    }>,
  activateInstance: (id: string) =>
    apiRequest("post", `/api/config/instances/${encodeURIComponent(id)}/activate`) as Promise<{
      ok: boolean;
      active_instance: string;
    }>,
  deleteInstance: (id: string) =>
    apiRequest("delete", `/api/config/instances/${encodeURIComponent(id)}`) as Promise<OkResponse>,

  // mods
  listMods: () => apiRequest("get", "/api/mods") as Promise<ModEntry[]>,
  setModsActive: (enable: string[], disable: string[], sort = false) =>
    apiRequest("post", "/api/mods/active", { body: { enable, disable, sort } }),
  getModHealth: () => apiRequest("get", "/api/mods/health") as Promise<ModHealthResult>,
  getModCompatibility: () =>
    apiRequest("get", "/api/mods/compatibility") as Promise<ModCompatibility>,
  checkUpdates: () =>
    apiRequest("get", "/api/mods/check-updates") as Promise<CheckUpdatesResult>,
  exportMods: () => apiRequest("get", "/api/mods/export") as Promise<unknown>,

  // queue
  getQueue: () => apiRequest("get", "/api/queue") as Promise<QueueState>,
  addToQueue: (ids: string[], force = false) =>
    apiRequest("post", "/api/queue/add", { body: { ids, force } }) as Promise<{
      added?: number;
      items?: QueueItem[];
    }>,
  startQueue: () => apiRequest("post", "/api/queue/start") as Promise<OkResponse>,
  clearQueue: () => apiRequest("post", "/api/queue/clear") as Promise<OkResponse>,
  pauseQueue: () => apiRequest("post", "/api/queue/pause") as Promise<{ paused?: boolean }>,
  resumeQueue: () => apiRequest("post", "/api/queue/resume") as Promise<{ paused?: boolean }>,
  removeQueueItem: (id: string, instanceId?: string | null) =>
    apiRequest("delete", `/api/queue/${encodeURIComponent(id)}`, {
      query: { instance_id: instanceId },
    }) as Promise<OkResponse>,
  retryQueue: (ids: string[] = [], instanceId?: string | null) =>
    apiRequest("post", "/api/queue/retry", {
      body: { ids },
      query: { instance_id: instanceId },
    }) as Promise<{
      accepted?: number;
    }>,

  // updates
  runAutoUpdate: () => apiRequest("post", "/api/auto-update/run") as Promise<AutoUpdateResult>,
  getAutoUpdateStatus: () =>
    apiRequest("get", "/api/auto-update/status") as Promise<AutoUpdateStatus>,
} as const;

export type UpdateRow = UpdateItem;
