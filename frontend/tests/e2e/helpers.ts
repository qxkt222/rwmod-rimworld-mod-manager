/**
 * Shared Playwright fixtures for the rwmod E2E suite.
 *
 * `backend` is worker-scoped: with `workers: 1` the whole run talks to one
 * real uvicorn process whose HOME is a throwaway temp dir (see
 * tests/fixtures/backend.ts). Tests use `backend.baseURL` explicitly because
 * the port is chosen at runtime.
 *
 * React is the only frontend entry since the cutover (2026-11-07): every spec
 * navigates to `/`, and the shell is a hash router (`/#<panel>`).
 */
import { test as base, expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import { startBackend, type BackendServer } from "../fixtures/backend";

export const test = base.extend<Record<string, never>, { backend: BackendServer }>({
  backend: [
    async ({}, use) => {
      const backend = await startBackend();
      await use(backend);
      await backend.stop();
    },
    { scope: "worker" },
  ],
});

export { expect };

/** Absolute URL of the React entry, optionally deep-linked to a panel. */
export function entryURL(backend: BackendServer, panel?: string): string {
  return `${backend.baseURL}/${panel ? `#${panel}` : ""}`;
}

/** Navigate to a panel via its hash URL and wait until it is the active one. */
export async function gotoPanel(page: Page, backend: BackendServer, panel: string): Promise<void> {
  await page.goto(entryURL(backend, panel));
  await expect(page.locator(`#panel-${panel}`)).toHaveClass(/active/);
}

/** The sidebar navigation control for a panel (canonical `nav-<name>` testid). */
export function navControl(page: Page, panel: string): Locator {
  return page.getByTestId(`nav-${panel}`);
}

/**
 * Click a labelled button inside an in-app dialog (`dialog-<name>`), asserting
 * the dialog actually opened first.
 */
export async function clickDialogButton(page: Page, name: string, label: string): Promise<void> {
  const dialog = page.getByTestId(`dialog-${name}`);
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: label }).click();
}

/** Assert a toast with the given text appeared in the toast container. */
export async function expectToast(page: Page, text: string | RegExp): Promise<void> {
  await expect(page.getByTestId("status-toasts")).toContainText(text);
}

/** Thin typed wrapper around Playwright's APIRequestContext. */
export async function apiGet<T>(request: APIRequestContext, backend: BackendServer, path: string): Promise<T> {
  const response = await request.get(`${backend.baseURL}${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}

export async function apiPost<T>(
  request: APIRequestContext,
  backend: BackendServer,
  path: string,
  data?: unknown,
): Promise<T> {
  const response = await request.post(`${backend.baseURL}${path}`, { data });
  expect(response.ok(), `POST ${path} → ${response.status()}`).toBeTruthy();
  return (await response.json()) as T;
}
