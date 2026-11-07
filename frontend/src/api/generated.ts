/**
 * AUTO-GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Source: the FastAPI app in `src/rwmod/server.py` (`app.openapi()`).
 * Regenerate: `npm run gen:api` (needs Python 3.13 + the repo's .venv).
 *
 * Usage in React components:
 *
 *   import type { components, paths } from "../api/generated";
 *
 *   // Request/response models declared by the backend:
 *   type Config = components["schemas"]["ConfigResponse"];
 *
 *   // A single endpoint's JSON response for status 200:
 *   type ModsResponse =
 *     paths["/api/mods"]["get"]["responses"][200]["content"]["application/json"];
 *
 * Endpoints whose handler returns an untyped dict (no FastAPI
 * `response_model`) map to `unknown` — that mirrors the backend's current
 * contract, not a generator bug. Add a response_model to tighten it.
 */

/** HTTP methods and route map, keyed by the backend's OpenAPI paths. */
export interface paths {
  "/": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/auto-check/clear": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/auto-check/result": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/auto-update/run": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/auto-update/status": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/backups": {
    get: {
      parameters: {
        query?: { workshop_id?: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["BackupListResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/backups/cleanup": {
    post: {
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["BackupCleanupRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["BackupCleanupResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/backups/{filename}": {
    delete: {
      parameters: {
        path: { filename: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["BackupDeleteResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/backups/{workshop_id}/restore": {
    post: {
      parameters: {
        path: { workshop_id: string };
      };
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["BackupRestoreRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["BackupRestoreResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/collection/preview/{collection_id}": {
    get: {
      parameters: {
        path: { collection_id: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/compat/check": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/config": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ConfigResponse"];
          };
        };
      };
    };
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ConfigUpdateRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/config/instances": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["InstanceCreateRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/config/instances/{instance_id}": {
    delete: {
      parameters: {
        path: { instance_id: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/config/instances/{instance_id}/activate": {
    post: {
      parameters: {
        path: { instance_id: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/dashboard": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["DashboardResponse"];
          };
        };
      };
    };
  };
  "/api/download": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["DownloadRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/download/stream": {
    get: {
      parameters: {
        query?: { force?: boolean; id: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/history": {
    get: {
      parameters: {
        query?: { limit?: number; status?: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["HistoryResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/history/clear": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/history/stats": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["DownloadStatsResponse"];
          };
        };
      };
    };
  };
  "/api/import/collection": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["CollectionImportRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/import/file": {
    post: {
      parameters: {
        query?: { force?: boolean };
      };
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_import_file_api_import_file_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/import/sort": {
    post: {
      parameters: {
        query?: { force?: boolean };
      };
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_import_sort_api_api_import_sort_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/locale": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["LocaleRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mod-settings": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingsListResponse"];
          };
        };
      };
    };
  };
  "/api/mod-settings/backup-all": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingBackupAllResponse"];
          };
        };
      };
    };
  };
  "/api/mod-settings/backups": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingBackupListResponse"];
          };
        };
      };
    };
  };
  "/api/mod-settings/{filename}": {
    get: {
      parameters: {
        path: { filename: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingContentResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
    put: {
      parameters: {
        path: { filename: string };
      };
      requestBody: {
        content: {
          "application/json": components["schemas"]["ModSettingWriteRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingWriteResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mod-settings/{filename}/restore": {
    post: {
      parameters: {
        path: { filename: string };
      };
      requestBody: {
        content: {
          "application/json": components["schemas"]["ModSettingRestoreRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ModSettingRestoreResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": Array<components["schemas"]["ModListItem"]>;
          };
        };
      };
    };
  };
  "/api/mods/active": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ActiveModsRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods/batch/delete": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["FoldersRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods/batch/download": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["DownloadRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods/check-updates": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/mods/compatibility": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/mods/dependencies": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ModIdsRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods/export": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/mods/export-collection": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/mods/health": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/mods/import-local": {
    post: {
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_import_local_mod_api_mods_import_local_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/mods/localization": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/onboarding/check": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/profiles": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ProfileListResponse"];
          };
        };
      };
    };
  };
  "/api/profiles/save": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ProfileSaveRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ProfileSaveResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/profiles/{name}": {
    delete: {
      parameters: {
        path: { name: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["OkResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/profiles/{name}/restore": {
    post: {
      parameters: {
        path: { name: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["ProfileSaveResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/queue": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["QueueStateResponse"];
          };
        };
      };
    };
  };
  "/api/queue/add": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ModIdsRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/queue/clear": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/queue/pause": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/queue/resume": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/queue/retry": {
    post: {
      parameters: {
        query?: { instance_id?: (string | null) };
      };
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["ModIdsRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/queue/start": {
    post: {
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["QueueStartRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/queue/{mod_id}": {
    delete: {
      parameters: {
        path: { mod_id: string };
        query?: { instance_id?: (string | null) };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/apply": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["RimsortApplyResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/check-order": {
    get: {
      parameters: {
        query?: { refresh_rules?: boolean };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["LoadOrderResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/compare": {
    post: {
      requestBody: {
        content: {
          "application/json": components["schemas"]["ModsConfigRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["RimsortCompareResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/compare-file": {
    post: {
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_rimsort_compare_file_api_rimsort_compare_file_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["RimsortCompareResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/generate": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["RimsortGenerateResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/sort": {
    post: {
      parameters: {
        query?: { refresh_rules?: boolean };
      };
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["RimsortSortRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["RimsortSortResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/steamdb/refresh": {
    post: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SteamDbRefreshResponse"];
          };
        };
      };
    };
  };
  "/api/rimsort/steamdb/status": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SteamDbStatusResponse"];
          };
        };
      };
    };
  };
  "/api/saves": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SaveListResponse"];
          };
        };
      };
    };
  };
  "/api/saves/analyze": {
    post: {
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_api_upload_and_analyze_api_saves_analyze_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SaveAnalyzeResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/saves/{save_name}": {
    get: {
      parameters: {
        path: { save_name: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SaveDetailResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/saves/{save_name}/download-missing": {
    post: {
      parameters: {
        path: { save_name: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/search": {
    get: {
      parameters: {
        query?: { page?: number; q?: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["SearchResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/status": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/steamcmd/check": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/tags": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["TagListResponse"];
          };
        };
      };
    };
  };
  "/api/tags/by-tag/{tag}": {
    get: {
      parameters: {
        path: { tag: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["TagFoldersResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/tags/{mod_folder}": {
    get: {
      parameters: {
        path: { mod_folder: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["FolderTagsResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
    post: {
      parameters: {
        path: { mod_folder: string };
      };
      requestBody: {
        content: {
          "application/json": components["schemas"]["TagRequest"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["TagMutationResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
    delete: {
      parameters: {
        path: { mod_folder: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["TagRemoveAllResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/tags/{mod_folder}/{tag}": {
    delete: {
      parameters: {
        path: { mod_folder: string; tag: string };
      };
      responses: {
        200: {
          content: {
            "application/json": components["schemas"]["OkResponse"];
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/transfer/export": {
    post: {
      requestBody?: {
        content: {
          "application/json": (components["schemas"]["TransferExportRequest"] | null);
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/transfer/import": {
    post: {
      requestBody: {
        content: {
          "multipart/form-data": components["schemas"]["Body_api_import_api_transfer_import_post"];
        };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/api/undo": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
    post: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
  "/api/workshop/{mod_id}": {
    get: {
      parameters: {
        path: { mod_id: string };
      };
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
        422: {
          content: {
            "application/json": components["schemas"]["ErrorResponse"];
          };
        };
      };
    };
  };
  "/metrics": {
    get: {
      responses: {
        200: {
          content: {
            "application/json": unknown;
          };
        };
      };
    };
  };
}

/** Every Pydantic model the backend exposes through OpenAPI. */
export interface components {
  schemas: {
    "ActiveModsRequest": { disable?: Array<string>; enable?: Array<string>; sort?: boolean };
    "BackupCleanupRequest": { keep?: number };
    "BackupCleanupResponse": { deleted?: number; ok?: boolean; [key: string]: unknown };
    "BackupDeleteResponse": { ok?: boolean; [key: string]: unknown };
    "BackupItem": { filename: string; folder_name: string; size_mb: number; timestamp: string; workshop_id: string; [key: string]: unknown };
    "BackupListResponse": { backup_dir?: string; backups?: Array<components["schemas"]["BackupItem"]>; [key: string]: unknown };
    "BackupRestoreRequest": { filename?: (string | null) };
    "BackupRestoreResponse": { msg?: string; ok?: boolean; restored_folder?: (string | null); [key: string]: unknown };
    "Body_api_import_api_transfer_import_post": { file: string };
    "Body_api_upload_and_analyze_api_saves_analyze_post": { file: string };
    "Body_import_file_api_import_file_post": { file: string };
    "Body_import_local_mod_api_mods_import_local_post": { file: string };
    "Body_import_sort_api_api_import_sort_post": { file: string };
    "Body_rimsort_compare_file_api_rimsort_compare_file_post": { file: string };
    "CollectionImportRequest": { collection_id?: string; force?: boolean };
    "ConfigResponse": { active_instance?: string; backup_dir: string; has_steam_api_key: boolean; instances?: Array<components["schemas"]["InstanceInfo"]>; mods_dir: string; mods_dir_exists: boolean; rimworld_dir: string; steamcmd_exists: boolean; steamcmd_path: string };
    "ConfigUpdateRequest": { backup_dir?: (string | null); mods_dir?: (string | null); rimworld_dir?: (string | null); steam_api_key?: (string | null); steamcmd_path?: (string | null) };
    "DashboardHealth": { abandoned?: number; removed?: number; stale?: number; stale_data?: boolean; [key: string]: unknown };
    "DashboardResponse": { disk_usage_mb?: number; health: components["schemas"]["DashboardHealth"]; mods_count?: number; recent_activity?: Array<components["schemas"]["HistoryItem"]>; size_pending?: boolean; updates_pending?: number; [key: string]: unknown };
    "DownloadRequest": { force?: boolean; ids?: Array<string> };
    "DownloadStatsResponse": { failed?: number; success?: number; total?: number; [key: string]: unknown };
    "ErrorResponse": { detail: string; error: string };
    "FolderTagsResponse": { folder: string; tags?: Array<string>; [key: string]: unknown };
    "FoldersRequest": { folders?: Array<string> };
    "HTTPValidationError": { detail?: Array<components["schemas"]["ValidationError"]> };
    "HistoryItem": { created_at?: string; id: number; instance_id?: string; mod_name?: string; msg?: string; package_id?: string; status?: string; workshop_id?: string; [key: string]: unknown };
    "HistoryResponse": { items?: Array<components["schemas"]["HistoryItem"]>; [key: string]: unknown };
    "InstanceCreateRequest": { backup_dir?: (string | null); config_dir?: (string | null); id?: (string | null); mods_dir: string; name?: (string | null); rimworld_dir?: (string | null); steamcmd_path?: (string | null) };
    "InstanceInfo": { backup_dir: string; config_dir?: (string | null); id: string; mods_dir: string; name: string; rimworld_dir: string; steamcmd_path: string };
    "LoadOrderIssue": { message: string; severity: string; [key: string]: unknown };
    "LoadOrderResponse": { issues?: Array<components["schemas"]["LoadOrderIssue"]>; load_order?: Array<string>; ok?: boolean; steam_db?: (components["schemas"]["LoadOrderSteamDb"] | null); total_mods?: number; [key: string]: unknown };
    "LoadOrderSteamDb": { game_versions?: Record<string, Array<string>>; matched?: number; unsupported?: Array<Record<string, unknown>>; [key: string]: unknown };
    "LocaleRequest": { locale?: string };
    "MissingModDetail": { package_id: string; resolved?: boolean; workshop_id?: string; [key: string]: unknown };
    "ModIdsRequest": { force?: boolean; ids?: Array<string> };
    "ModListItem": { active?: boolean; folder: string; name: string; package_id: string; size_mb?: number; size_pending?: boolean; tags?: Array<string>; workshop_id: string; [key: string]: unknown };
    "ModSearchItem": { author: string; description: string; id: string; installed: boolean; preview_url: string; rating: string; subscribers: string; title: string };
    "ModSettingBackup": { filename: string; mtime?: string; size?: number; source?: string; [key: string]: unknown };
    "ModSettingBackupAllResponse": { backup_dir?: string; backups?: Array<components["schemas"]["ModSettingBackupRow"]>; count?: number; ok?: boolean; [key: string]: unknown };
    "ModSettingBackupListResponse": { backup_dir?: string; backups?: Array<components["schemas"]["ModSettingBackup"]>; [key: string]: unknown };
    "ModSettingBackupRow": { backup?: string; filename: string; size?: number; [key: string]: unknown };
    "ModSettingContentResponse": { filename: string; game_running?: boolean; mtime?: string; root_tag?: string; size?: number; well_formed?: boolean; xml?: string; [key: string]: unknown };
    "ModSettingFile": { associated?: boolean; filename: string; mtime?: string; name?: string; package_id?: string; size?: number; workshop_id?: string; [key: string]: unknown };
    "ModSettingRestoreRequest": { ack_game_closed?: boolean; backup?: string };
    "ModSettingRestoreResponse": { backup?: string; filename: string; game_running?: boolean; ok?: boolean; restored_from?: string; size?: number; warning?: string; [key: string]: unknown };
    "ModSettingWriteRequest": { ack_game_closed?: boolean; xml?: string };
    "ModSettingWriteResponse": { backup?: string; filename: string; game_running?: boolean; ok?: boolean; size?: number; warning?: string; [key: string]: unknown };
    "ModSettingsListResponse": { config_dir?: (string | null); files?: Array<components["schemas"]["ModSettingFile"]>; [key: string]: unknown };
    "ModsConfigRequest": { xml?: string };
    "OkResponse": { ok?: boolean };
    "ProfileItem": { mod_count?: number; name: string; saved_at?: string; size_kb?: number; [key: string]: unknown };
    "ProfileListResponse": { modsconfig_path?: (string | null); profiles?: Array<components["schemas"]["ProfileItem"]>; [key: string]: unknown };
    "ProfileSaveRequest": { name?: string };
    "ProfileSaveResponse": { mod_count?: number; msg?: string; ok?: boolean; [key: string]: unknown };
    "QueueItemState": { downloaded?: number; error_detail?: string; error_kind?: string; id: string; instance_id?: string; msg?: string; name?: string; progress?: number; speed_bps?: number; status?: string; total?: number; [key: string]: unknown };
    "QueueStartRequest": { force?: boolean };
    "QueueStateResponse": { items?: Array<components["schemas"]["QueueItemState"]>; paused?: boolean; [key: string]: unknown };
    "RimsortApplyResponse": { backup?: string; cycles?: Array<Array<string>>; msg?: string; ok?: boolean; sorted?: Array<string>; [key: string]: unknown };
    "RimsortCompareResponse": { extra?: Array<string>; extra_count?: number; installed?: Array<string>; installed_count?: number; load_order?: Array<string>; missing?: Array<string>; missing_count?: number; missing_details?: Array<components["schemas"]["MissingModDetail"]>; total_in_config?: number; [key: string]: unknown };
    "RimsortGenerateResponse": { modsconfig_xml?: string; [key: string]: unknown };
    "RimsortSortRequest": { active_ids?: (Array<string> | null) };
    "RimsortSortResponse": { cycles?: Array<Array<string>>; rimworld_version?: string; sorted?: Array<string>; unresolved?: Array<string>; violations?: Array<components["schemas"]["SortViolation"]>; [key: string]: unknown };
    "SaveAnalyzeResponse": { filename?: (string | null); mod_count?: number; mods?: Array<string>; [key: string]: unknown };
    "SaveDetailResponse": { completeness?: number; file: string; game_version?: (string | null); loadable?: boolean; missing_mods?: Array<string>; name: string; required_mods?: Array<string>; total_mods?: number; unused_by_save?: Array<string>; [key: string]: unknown };
    "SaveListResponse": { saves?: Array<components["schemas"]["SaveSummary"]>; [key: string]: unknown };
    "SaveSummary": { completeness?: number; game_version?: (string | null); loadable?: boolean; missing_count?: number; name: string; total_mods?: number; [key: string]: unknown };
    "SearchResponse": { results: Array<components["schemas"]["ModSearchItem"]> };
    "SortViolation": { kind?: string; reason?: string; [key: string]: unknown };
    "SteamDbRefreshResponse": { cached?: boolean; entries?: number; max_bytes?: number; ok?: boolean; size?: number; stale?: boolean; ttl_seconds?: number; updated_at?: (string | null); valid?: boolean; version?: (string | number | null); [key: string]: unknown };
    "SteamDbStatusResponse": { cached?: boolean; enabled?: boolean; entries?: number; max_bytes?: number; size?: number; stale?: boolean; ttl_seconds?: number; updated_at?: (string | null); valid?: boolean; version?: (string | number | null); [key: string]: unknown };
    "TagCount": { count?: number; tag: string; [key: string]: unknown };
    "TagFoldersResponse": { folders?: Array<string>; tag: string; [key: string]: unknown };
    "TagListResponse": { tags?: Array<components["schemas"]["TagCount"]>; [key: string]: unknown };
    "TagMutationResponse": { folder?: string; ok?: boolean; tag?: string; [key: string]: unknown };
    "TagRemoveAllResponse": { ok?: boolean; removed?: number; [key: string]: unknown };
    "TagRequest": { tag?: string };
    "TransferExportRequest": { include_backups?: boolean; include_secrets?: boolean };
    "ValidationError": { ctx?: Record<string, unknown>; input?: Record<string, unknown>; loc: Array<(string | number)>; msg: string; type: string };
  };
}

/** Union of all schema names — handy for generic helpers. */
export type SchemaName = keyof components["schemas"];
