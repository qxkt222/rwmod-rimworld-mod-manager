/**
 * Auth helpers — token management + login overlay.
 *
 * The backend enforces JWT auth on every /api route. The signing key comes
 * from the RWMOD_SECRET env var, or — when unset — from a random key that is
 * generated on first start and persisted to rwmod.secret in the config dir
 * (stable across restarts). This module:
 *   1. Verifies the stored token against GET /api/auth/verify.
 *   2. If invalid/missing, shows a login overlay so the user can enter the
 *      access secret (printed in the server startup log / rwmod.secret).
 *   3. Stores the token in localStorage and injects it into every /api fetch
 *      (see patchFetchWithAuth in main.ts).
 */

const TOKEN_KEY = "rwmod_token";

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
 * Returns true if authenticated, false if the user cancelled the login overlay.
 */
export async function ensureAuth(): Promise<boolean> {
  if (await verifyToken()) return true;
  clearToken();
  return showLoginOverlay();
}

// ── login overlay ──────────────────────────────────────────────────

let overlayPromise: Promise<boolean> | null = null;
/** 用户点「取消」后不再自动弹窗（避免 401 触发的弹窗循环），刷新页面可重新登录。 */
let overlayDismissed = false;

/**
 * Show the login overlay and wait for the user to enter the access secret.
 * Re-entrant: concurrent calls share the same pending promise.
 */
export function showLoginOverlay(): Promise<boolean> {
  if (overlayDismissed) return Promise.resolve(false);
  if (overlayPromise) return overlayPromise;

  overlayPromise = new Promise((resolve) => {
    const overlay = document.getElementById("login-overlay");
    const input = document.getElementById("login-password") as HTMLInputElement | null;
    const btn = document.getElementById("login-submit") as HTMLButtonElement | null;
    const err = document.getElementById("login-error");
    const cancel = document.getElementById("login-cancel") as HTMLButtonElement | null;

    if (!overlay || !input || !btn) {
      overlayPromise = null;
      resolve(false);
      return;
    }
    overlay.classList.add("active");
    input.focus();

    const finish = (ok: boolean) => {
      overlay.classList.remove("active");
      overlayPromise = null;
      resolve(ok);
    };

    const doLogin = async () => {
      btn.disabled = true;
      if (err) err.textContent = "";
      const ok = await login(input.value);
      if (ok) {
        overlayDismissed = false;
        finish(true);
      } else {
        if (err) err.textContent = "密钥错误，请重试（见启动日志或 rwmod.secret 文件）";
        btn.disabled = false;
        input.focus();
      }
    };

    btn.addEventListener("click", doLogin);
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doLogin();
    });
    cancel?.addEventListener("click", () => {
      overlayDismissed = true;
      finish(false);
    });
  });

  return overlayPromise;
}
