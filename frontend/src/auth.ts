/**
 * Auth helpers — automatic login + token management.
 *
 * The backend enforces JWT auth on every /api route. This module:
 *   1. Tries to auto-login with the default dev secret (so a fresh install
 *      "just works" for normal players who never set RWMOD_SECRET).
 *   2. If that fails (user changed RWMOD_SECRET), shows a login overlay so
 *      they can enter the password manually.
 *   3. Stores the token in localStorage and injects it into every /api fetch.
 */

const TOKEN_KEY = "rwmod_token";
const DEFAULT_SECRET = "rwmod-dev-secret";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Call the backend login endpoint and store the returned token. */
export async function login(password: string): Promise<boolean> {
  const resp = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!resp.ok) return false;
  const data = await resp.json();
  if (!data.token) return false;
  setToken(data.token);
  return true;
}

/** Try to auto-login with the default dev secret. */
export async function autoLogin(): Promise<boolean> {
  // If we already have a valid token, skip.
  if (getToken()) {
    const ok = await verifyToken();
    if (ok) return true;
    clearToken();
  }
  return login(DEFAULT_SECRET);
}

/** Check whether the stored token is still valid. */
export async function verifyToken(): Promise<boolean> {
  const token = getToken();
  if (!token) return false;
  const resp = await fetch("/api/auth/verify", {
    headers: { Authorization: `Bearer ${token}` },
  });
  return resp.ok;
}

/**
 * Ensure we are authenticated before the UI loads.
 * Returns true if authenticated, false if the user must log in manually.
 */
export async function ensureAuth(): Promise<boolean> {
  if (await autoLogin()) return true;
  return showLoginOverlay();
}

/** Show a login overlay and wait for the user to enter the password. */
function showLoginOverlay(): Promise<boolean> {
  return new Promise((resolve) => {
    const overlay = document.getElementById("login-overlay");
    if (!overlay) {
      resolve(false);
      return;
    }
    overlay.classList.add("active");

    const input = document.getElementById("login-password") as HTMLInputElement | null;
    const btn = document.getElementById("login-submit") as HTMLButtonElement | null;
    const err = document.getElementById("login-error");
    const cancel = document.getElementById("login-cancel") as HTMLButtonElement | null;

    const doLogin = async () => {
      if (!input || !btn) return;
      btn.disabled = true;
      const ok = await login(input.value);
      if (ok) {
        overlay.classList.remove("active");
        resolve(true);
      } else {
        if (err) err.textContent = "密码错误，请重试";
        btn.disabled = false;
      }
    };

    btn?.addEventListener("click", doLogin);
    input?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doLogin();
    });
    input?.focus();

    // Cancel = give up (UI stays mostly empty, but avoids a dead overlay).
    cancel?.addEventListener("click", () => {
      overlay.classList.remove("active");
      resolve(false);
    });
  });
}
