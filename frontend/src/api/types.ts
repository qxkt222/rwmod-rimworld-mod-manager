/**
 * Narrowed API types for the React client.
 *
 * `generated.ts` is the machine-readable source of truth, but endpoints that
 * still return a bare `dict` map to `unknown` there. This file pins the types
 * actually consumed by the shared layer; every alias below either points at a
 * generated schema or documents the exact hand-written shape of an untyped
 * endpoint. When a backend router adds a `response_model`, swap the hand
 * written interface for `Schemas[...]` and the compiler will enforce it.
 */
import type { components } from "./generated";

type Schemas = components["schemas"];

// ── generated schemas ────────────────────────────────────────────
export type DashboardData = Schemas["DashboardResponse"];
export type DashboardHealth = Schemas["DashboardHealth"];
export type HistoryItem = Schemas["HistoryItem"];
export type QueueItem = Schemas["QueueItemState"];
export type QueueState = Schemas["QueueStateResponse"];
export type ConfigData = Schemas["ConfigResponse"];
export type InstanceData = Schemas["InstanceInfo"];
export type ModEntry = Schemas["ModListItem"];
export type ModSettingFile = Schemas["ModSettingFile"];
export type ErrorResponse = Schemas["ErrorResponse"];
export type OkResponse = Schemas["OkResponse"];

// ── hand-narrowed shapes (endpoints without response_model) ──────

/** `GET /api/status` — `{ online: boolean }`. */
export interface AppStatus {
  online: boolean;
}

/** `GET /api/steamcmd/check` — probe endpoint, 200 even on failure. */
export interface SteamcmdCheck {
  ok?: boolean;
  msg?: string;
}

/**
 * `POST /api/auto-update/run` — 200 body.
 *
 * A check that is already running is a 409 (`ConflictError`), never a 200 with
 * `ok:false`; the client branches on `ApiError.status` instead.
 */
export interface AutoUpdateResult {
  checked?: number;
  outdated?: number;
  queued?: number;
}

/** `GET /api/auto-update/status`. */
export interface AutoUpdateStatus {
  running: boolean;
}

/** One row of `GET /api/mods/check-updates`. */
export interface UpdateItem {
  workshop_id: string;
  name: string;
  folder: string;
  remote_title?: string;
  time_updated?: number;
  file_description?: string;
}

/**
 * `GET /api/mods/check-updates`.
 *
 * A check whose Steam batches partially failed still answers 200: `partial`
 * marks the list as incomplete and the batch counters say how incomplete.
 * Only a check where every batch failed is a 502.
 */
export interface CheckUpdatesResult {
  updates?: UpdateItem[];
  partial?: boolean;
  failed_batches?: number;
  total_batches?: number;
}

/** `GET /api/mods/health` — `{ mods?: { folder, status }[] }`. */
export interface ModHealthResult {
  mods?: { folder: string; status: string }[];
}

/** `GET /api/mods/compatibility`. */
export interface ModCompatibility {
  rimworld_version?: string | null;
  groups?: {
    incompatible?: { folder: string }[];
    unknown?: { folder: string }[];
  };
  [key: string]: unknown;
}

/** `GET /api/queue` is typed; the WS payload carries the same item shape. */
export type QueueItems = QueueItem[];
