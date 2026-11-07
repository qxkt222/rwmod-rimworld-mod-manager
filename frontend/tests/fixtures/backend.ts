/**
 * Real-backend fixture for the Playwright E2E suite.
 *
 * Contract (FE-TEST task book):
 *  - spawn the *real* FastAPI app with uvicorn on a random free port;
 *  - point HOME / USERPROFILE / RWMOD_TEST_SANDBOX_HOME at a fresh temp dir so
 *    the user's real ~/.rwmod.toml, ~/.rwmod.db, ~/.rwmod.log and
 *    ~/AppData/LocalLow/Ludeon Studios are never read or written;
 *  - seed an empty-but-valid configuration (empty Mods dir, empty backup dir,
 *    a dummy steamcmd.exe) so no real SteamCMD / RimWorld install is touched;
 *  - poll /api/status until the server answers, and fail loudly with the
 *    uvicorn log if it dies early;
 *  - kill the process tree and delete the temp home on teardown.
 *
 * The frontend served by this backend is the real `vite build` output in
 * <repo>/static — nothing about the UI is mocked.
 */
import { spawn, type ChildProcess } from "node:child_process";
import {
  createWriteStream,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));

/** <repo>/frontend */
export const FRONTEND_DIR = path.resolve(HERE, "../..");
/** <repo> */
export const REPO_ROOT = path.resolve(FRONTEND_DIR, "..");
export const STATIC_INDEX = path.join(REPO_ROOT, "static", "index.html");

export interface BackendServer {
  /** e.g. http://127.0.0.1:54321 — includes the random port, no trailing slash. */
  baseURL: string;
  /** Throwaway home directory used for this server (also the sandbox root). */
  home: string;
  port: number;
  /** Path of the uvicorn stdout/stderr capture, useful in failure messages. */
  logPath: string;
  stop(): Promise<void>;
}

const MODSCONFIG_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModsConfigData>
  <version>1.5.4104 rev435</version>
  <activeMods>
    <li>Ludeon.RimWorld</li>
  </activeMods>
  <knownExpansions />
</ModsConfigData>
`;

const MOD_SETTINGS_XML = `<?xml version="1.0" encoding="utf-8"?>
<ModSettings>
  <example>1</example>
</ModSettings>
`;

function pickFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.unref();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const address = probe.address();
      const port = typeof address === "object" && address ? address.port : 0;
      probe.close(() => {
        if (port > 0) resolve(port);
        else reject(new Error("could not allocate a free TCP port"));
      });
    });
  });
}

/** Prefer the project venv (same interpreter run_tests.py uses). */
function pythonCommand(): string {
  const venv =
    process.platform === "win32"
      ? path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
      : path.join(REPO_ROOT, ".venv", "bin", "python");
  return existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3";
}

function seedHome(home: string): void {
  for (const dir of ["mods", "mods2", "rimworld", "rimworld2", "config", "config/Saves", "backups", "steamcmd"]) {
    mkdirSync(path.join(home, dir), { recursive: true });
  }

  // A cheap regular file is enough for /api/config's existence check; the E2E
  // suite never executes it (no real SteamCMD runs are allowed).
  writeFileSync(path.join(home, "steamcmd", "steamcmd.exe"), "", "utf8");
  writeFileSync(path.join(home, "config", "ModsConfig.xml"), MODSCONFIG_XML, "utf8");
  writeFileSync(path.join(home, "config", "Mod_TestExample.xml"), MOD_SETTINGS_XML, "utf8");

  // v1 flat config (Config.load() synthesises the default instance from it).
  // Forward slashes keep TOML simple and work on both Windows and POSIX.
  const posix = (p: string) => p.replace(/\\/g, "/");
  writeFileSync(
    path.join(home, ".rwmod.toml"),
    [
      `steamcmd_path = "${posix(path.join(home, "steamcmd", "steamcmd.exe"))}"`,
      `mods_dir = "${posix(path.join(home, "mods"))}"`,
      `rimworld_dir = "${posix(path.join(home, "rimworld"))}"`,
      `backup_dir = "${posix(path.join(home, "backups"))}"`,
      `config_dir = "${posix(path.join(home, "config"))}"`,
      "",
    ].join("\n"),
    "utf8",
  );
}

function lastLogLines(logPath: string, count = 40): string {
  try {
    return readFileSync(logPath, "utf8").split(/\r?\n/).slice(-count).join("\n");
  } catch {
    return "<no uvicorn log yet>";
  }
}

async function waitForReady(baseURL: string, child: ChildProcess, logPath: string): Promise<void> {
  const deadline = Date.now() + 45_000;
  let lastError = "no response";
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(
        `uvicorn exited before becoming ready (code ${child.exitCode}):\n${lastLogLines(logPath)}`,
      );
    }
    try {
      const response = await fetch(`${baseURL}/api/status`, {
        signal: AbortSignal.timeout(1_500),
      });
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error(
    `backend did not become ready within 45s (${lastError}):\n${lastLogLines(logPath)}`,
  );
}

function waitForExit(child: ChildProcess, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      child.removeListener("exit", onExit);
      resolve();
    }, timeoutMs);
    timer.unref();
    const onExit = () => {
      clearTimeout(timer);
      resolve();
    };
    child.once("exit", onExit);
  });
}

export async function startBackend(): Promise<BackendServer> {
  // Fail with an actionable message instead of a 503 from GET /.
  if (!existsSync(STATIC_INDEX)) {
    throw new Error(
      `前端构建产物缺失: ${STATIC_INDEX}\n请先执行: cd frontend && npm run build`,
    );
  }

  const home = mkdtempSync(path.join(tmpdir(), "rwmod-e2e-"));
  seedHome(home);

  const port = await pickFreePort();
  const baseURL = `http://127.0.0.1:${port}`;
  const logPath = path.join(home, "uvicorn.log");
  const logStream = createWriteStream(logPath, { flags: "a" });

  const env: NodeJS.ProcessEnv = {
    ...process.env,
    // Path.home() on Windows reads USERPROFILE; on POSIX it reads HOME.
    HOME: home,
    USERPROFILE: home,
    // Fail-closed guard for ModsConfig.xml resolution/writes.
    RWMOD_TEST_SANDBOX_HOME: home,
    PYTHONPATH: [path.join(REPO_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
  };

  const child = spawn(
    pythonCommand(),
    [
      "-m",
      "uvicorn",
      "rwmod.server:app",
      "--host",
      "127.0.0.1",
      "--port",
      String(port),
      "--log-level",
      "warning",
    ],
    {
      cwd: REPO_ROOT,
      env,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  child.stdout?.pipe(logStream);
  child.stderr?.pipe(logStream);

  const stop = async (): Promise<void> => {
    if (child.exitCode === null && child.pid !== undefined) {
      if (process.platform === "win32") {
        await new Promise<void>((resolve) => {
          const killer = spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
            stdio: "ignore",
            windowsHide: true,
          });
          killer.once("exit", () => resolve());
          killer.once("error", () => resolve());
        });
      } else {
        child.kill("SIGTERM");
      }
      await waitForExit(child, 10_000);
      if (child.exitCode === null) child.kill("SIGKILL");
      await waitForExit(child, 2_000);
    }
    await new Promise<void>((resolve) => logStream.end(resolve));
    try {
      rmSync(home, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    } catch {
      // Temp-dir cleanup is best effort; the OS reaps %TEMP% on its own.
    }
  };

  try {
    await waitForReady(baseURL, child, logPath);
  } catch (error) {
    await stop();
    throw error;
  }

  // Safety net: never let a mis-seeded fixture point the app at real paths.
  const cfg = (await (await fetch(`${baseURL}/api/config`)).json()) as {
    mods_dir?: string;
    active_instance?: string;
  };
  const expectedMods = path.join(home, "mods");
  if (!cfg.mods_dir || path.resolve(cfg.mods_dir) !== path.resolve(expectedMods)) {
    await stop();
    throw new Error(
      `E2E isolation failed: backend mods_dir=${cfg.mods_dir!}, expected ${expectedMods}. ` +
        "Refusing to run against the real user profile.",
    );
  }

  return { baseURL, home, port, logPath, stop };
}
