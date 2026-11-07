#!/usr/bin/env node
/**
 * Generate `src/api/generated.ts` from the FastAPI OpenAPI schema.
 *
 * Why not fetch /api/openapi.json: generation must work offline and in CI
 * without booting uvicorn. Importing `rwmod.server:app` builds the same
 * schema object the server would serve, with the customised 422 body
 * (`ErrorResponse`) already applied.
 *
 * The Python process is isolated (temporary HOME + PYTHONPATH=<repo>/src) so
 * `npm run gen:api` never reads or writes the developer's real ~/.rwmod.*.
 *
 * Zero npm dependencies on purpose: the output is a small, deterministic
 * subset of OpenAPI (objects, arrays, unions, $ref, enums). Anything the
 * generator does not understand becomes `unknown` rather than silently
 * producing a wrong type.
 *
 * Usage: npm run gen:api
 */
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptsDir = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(scriptsDir, "..");
const repoRoot = resolve(frontendDir, "..");
const srcDir = join(repoRoot, "src");
const outFile = join(frontendDir, "src", "api", "generated.ts");

const HTTP_METHODS = ["get", "post", "put", "patch", "delete", "options", "head", "trace"];
const IDENT_RE = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
const NUMERIC_RE = /^\d+$/;

// ── schema → TypeScript ──────────────────────────────────────────────

function quoteKey(key) {
  return IDENT_RE.test(key) ? key : JSON.stringify(key);
}

function refName(ref) {
  const name = ref.split("/").pop();
  if (!name) throw new Error(`Malformed $ref: ${ref}`);
  return name;
}

/** Deduplicate union members and put `null` last. */
function unionOf(parts) {
  const seen = new Set();
  const unique = [];
  for (const part of parts) {
    if (!seen.has(part)) {
      seen.add(part);
      unique.push(part);
    }
  }
  unique.sort((a, b) => (a === "null" ? 1 : b === "null" ? -1 : 0));
  return unique.length === 1 ? unique[0] : `(${unique.join(" | ")})`;
}

function intersectionOf(parts) {
  const unique = [...new Set(parts)];
  return unique.length === 1 ? unique[0] : `(${unique.join(" & ")})`;
}

/** Convert one OpenAPI schema object to an inline TypeScript type. */
function tsType(schema) {
  if (!schema || typeof schema !== "object") return "unknown";
  // An empty schema object means "any JSON value" in OpenAPI.
  if (Object.keys(schema).length === 0) return "unknown";

  if (typeof schema.$ref === "string") {
    return `components["schemas"][${JSON.stringify(refName(schema.$ref))}]`;
  }
  if (Array.isArray(schema.anyOf)) return unionOf(schema.anyOf.map(tsType));
  if (Array.isArray(schema.oneOf)) return unionOf(schema.oneOf.map(tsType));
  if (Array.isArray(schema.allOf)) return intersectionOf(schema.allOf.map(tsType));
  if (Array.isArray(schema.enum)) {
    const values = schema.enum.map((value) => JSON.stringify(value));
    return values.length ? `(${values.join(" | ")})` : "never";
  }
  if (schema.const !== undefined) return JSON.stringify(schema.const);

  let base;
  switch (schema.type) {
    case "string":
      base = "string";
      break;
    case "integer":
    case "number":
      base = "number";
      break;
    case "boolean":
      base = "boolean";
      break;
    case "null":
      base = "null";
      break;
    case "array":
      base = `Array<${schema.items ? tsType(schema.items) : "unknown"}>`;
      break;
    case "object":
    case undefined:
      base = objectType(schema);
      break;
    default:
      base = "unknown";
  }
  return schema.nullable === true ? `${base} | null` : base;
}

function objectType(schema) {
  if (!schema.properties) {
    if (schema.additionalProperties && typeof schema.additionalProperties === "object") {
      return `Record<string, ${tsType(schema.additionalProperties)}>`;
    }
    return "Record<string, unknown>";
  }

  const required = new Set(schema.required ?? []);
  const members = Object.keys(schema.properties)
    .sort()
    .map((key) => {
      const optional = required.has(key) ? "" : "?";
      return `${quoteKey(key)}${optional}: ${tsType(schema.properties[key])}`;
    });

  const extra = schema.additionalProperties;
  if (extra === true) members.push("[key: string]: unknown");
  else if (extra && typeof extra === "object") members.push(`[key: string]: ${tsType(extra)}`);

  return members.length ? `{ ${members.join("; ")} }` : "Record<string, unknown>";
}

// ── paths → TypeScript ───────────────────────────────────────────────

function renderParameters(parameters) {
  if (!parameters?.length) return [];

  const groups = { path: [], query: [], header: [], cookie: [] };
  for (const parameter of parameters) {
    const bucket = groups[parameter.in] ?? (groups[parameter.in] = []);
    bucket.push(parameter);
  }

  const groupLines = [];
  for (const where of ["path", "query", "header", "cookie"]) {
    const params = groups[where];
    if (!params?.length) continue;
    const requiredByDefault = where === "path";
    const members = params
      .slice()
      .sort((a, b) => a.name.localeCompare(b.name))
      .map((parameter) => {
        const optional = parameter.required || requiredByDefault ? "" : "?";
        return `${quoteKey(parameter.name)}${optional}: ${tsType(parameter.schema)}`;
      });
    groupLines.push(`    ${where}${requiredByDefault ? "" : "?"}: { ${members.join("; ")} };`);
  }

  return ["  parameters: {", ...groupLines, "  };"];
}

function renderRequestBody(requestBody) {
  if (!requestBody?.content) return [];
  const content = Object.keys(requestBody.content)
    .sort()
    .map((mediaType) => {
      const schema = requestBody.content[mediaType]?.schema;
      return `      ${JSON.stringify(mediaType)}: ${tsType(schema)};`;
    });
  const optional = requestBody.required ? "" : "?";
  return [`  requestBody${optional}: {`, "    content: {", ...content, "    };", "  };"];
}

function renderResponses(responses) {
  if (!responses) return [];
  const codes = Object.keys(responses).sort((a, b) => {
    if (NUMERIC_RE.test(a) && NUMERIC_RE.test(b)) return Number(a) - Number(b);
    if (NUMERIC_RE.test(a)) return -1;
    if (NUMERIC_RE.test(b)) return 1;
    return a.localeCompare(b);
  });

  const body = [];
  for (const code of codes) {
    const response = responses[code];
    const key = NUMERIC_RE.test(code) ? code : JSON.stringify(code);
    const content = response?.content;
    if (!content || !Object.keys(content).length) {
      body.push(`    ${key}: unknown;`);
      continue;
    }
    const mediaLines = Object.keys(content)
      .sort()
      .map((mediaType) => `        ${JSON.stringify(mediaType)}: ${tsType(content[mediaType]?.schema)};`);
    body.push(`    ${key}: {`, "      content: {", ...mediaLines, "      };", "    };");
  }

  return ["  responses: {", ...body, "  };"];
}

function renderOperation(operation) {
  const lines = [
    ...renderParameters(operation.parameters),
    ...renderRequestBody(operation.requestBody),
    ...renderResponses(operation.responses),
  ];
  return lines.length ? ["{", ...lines, "};"] : ["{};"];
}

// ── top-level emit ───────────────────────────────────────────────────

function renderPaths(paths) {
  const lines = ["export interface paths {"];
  for (const route of Object.keys(paths).sort()) {
    const item = paths[route] ?? {};
    lines.push(`  ${JSON.stringify(route)}: {`);
    for (const method of HTTP_METHODS) {
      if (!item[method]) continue;
      const operation = renderOperation(item[method])
        .map((line) => `    ${line}`)
        .join("\n");
      lines.push(`    ${method}: ${operation.trimStart()}`);
    }
    lines.push("  };");
  }
  lines.push("}");
  return lines.join("\n");
}

function renderComponents(schemas) {
  const lines = ["export interface components {", "  schemas: {"];
  for (const name of Object.keys(schemas).sort()) {
    lines.push(`    ${JSON.stringify(name)}: ${tsType(schemas[name])};`);
  }
  lines.push("  };", "}");
  return lines.join("\n");
}

const HEADER = `/**
 * AUTO-GENERATED FILE — DO NOT EDIT BY HAND.
 *
 * Source: the FastAPI app in \`src/rwmod/server.py\` (\`app.openapi()\`).
 * Regenerate: \`npm run gen:api\` (needs Python 3.13 + the repo's .venv).
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
 * \`response_model\`) map to \`unknown\` — that mirrors the backend's current
 * contract, not a generator bug. Add a response_model to tighten it.
 */
`;

function main() {
  const python = pickPython();
  const workDir = mkdtempSync(join(tmpdir(), "rwmod-gen-api-"));
  const schemaPath = join(workDir, "openapi.json");

  try {
    const pythonCode = [
      "import json, sys",
      "from rwmod.server import app",
      "with open(sys.argv[1], 'w', encoding='utf-8') as fh:",
      "    json.dump(app.openapi(), fh, ensure_ascii=False)",
    ].join("\n");

    const env = {
      ...process.env,
      PYTHONPATH: [srcDir, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      PYTHONIOENCODING: "utf-8",
      // Keep init_logging() / Path.home() out of the developer's real profile.
      HOME: workDir,
      USERPROFILE: workDir,
    };

    const result = spawnSync(python, ["-c", pythonCode, schemaPath], {
      cwd: repoRoot,
      env,
      encoding: "utf-8",
    });
    if (result.error) {
      throw new Error(
        `Could not run Python (${python}). Set RWMOD_PYTHON to a Python 3.13 executable.`,
      );
    }
    if (result.status !== 0) {
      throw new Error(
        `Python schema export failed (exit ${result.status}).\n${result.stderr || result.stdout || ""}`,
      );
    }

    const schema = JSON.parse(readFileSync(schemaPath, "utf-8"));
    const schemas = schema.components?.schemas ?? {};
    const paths = schema.paths ?? {};

    const output = [
      HEADER,
      `/** HTTP methods and route map, keyed by the backend's OpenAPI paths. */`,
      renderPaths(paths),
      "",
      `/** Every Pydantic model the backend exposes through OpenAPI. */`,
      renderComponents(schemas),
      "",
      `/** Union of all schema names — handy for generic helpers. */`,
      `export type SchemaName = keyof components["schemas"];`,
      "",
    ].join("\n");

    mkdirSync(dirname(outFile), { recursive: true });
    writeFileSync(outFile, output, "utf-8");

    console.log(
      `[gen:api] wrote ${Object.keys(paths).length} paths / ` +
        `${Object.keys(schemas).length} schemas -> ${outFile}`,
    );
  } finally {
    rmSync(workDir, { recursive: true, force: true });
  }
}

function pickPython() {
  if (process.env.RWMOD_PYTHON) return process.env.RWMOD_PYTHON;

  const venv =
    process.platform === "win32"
      ? join(repoRoot, ".venv", "Scripts", "python.exe")
      : join(repoRoot, ".venv", "bin", "python");
  if (existsSync(venv)) return venv;

  return process.platform === "win32" ? "python" : "python3";
}

main();
