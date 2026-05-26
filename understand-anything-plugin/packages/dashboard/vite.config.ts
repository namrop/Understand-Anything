/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";
import fs from "fs";
import crypto from "crypto";

// Generate a one-time token when the server process starts.
// This token is printed to the terminal and must be in the URL
// to fetch knowledge-graph.json or diff-overlay.json.
const ACCESS_TOKEN = process.env.UNDERSTAND_ACCESS_TOKEN || crypto.randomBytes(16).toString("hex");
const AUTH_COOKIE_NAME = "understand_anything_auth";
const AUTH_COOKIE_MAX_AGE_SECONDS = Number(
  process.env.UNDERSTAND_AUTH_COOKIE_MAX_AGE_SECONDS ?? 60 * 60 * 24 * 90,
);
const AUTH_COOKIE_SECURE = process.env.UNDERSTAND_AUTH_COOKIE_SECURE === "true";
const ADDITIONAL_ALLOWED_HOSTS = (process.env.UNDERSTAND_ALLOWED_HOSTS ?? "")
  .split(",")
  .map((host) => host.trim())
  .filter(Boolean);
const MAX_SOURCE_FILE_BYTES = 1024 * 1024;

function parseCookies(cookieHeader: string | undefined): Map<string, string> {
  const cookies = new Map<string, string>();
  if (!cookieHeader) return cookies;
  for (const pair of cookieHeader.split(";")) {
    const separator = pair.indexOf("=");
    if (separator === -1) continue;
    const name = pair.slice(0, separator).trim();
    const value = pair.slice(separator + 1).trim();
    if (!name) continue;
    try {
      cookies.set(name, decodeURIComponent(value));
    } catch {
      cookies.set(name, value);
    }
  }
  return cookies;
}

function authCookieHeader(value: string, maxAgeSeconds: number): string {
  return [
    `${AUTH_COOKIE_NAME}=${encodeURIComponent(value)}`,
    "Path=/",
    `Max-Age=${maxAgeSeconds}`,
    "HttpOnly",
    "SameSite=Lax",
    ...(AUTH_COOKIE_SECURE ? ["Secure"] : []),
  ].join("; ");
}

function hasValidAccess(url: URL, req: import("http").IncomingMessage): boolean {
  const queryToken = url.searchParams.get("token");
  if (queryToken === ACCESS_TOKEN) return true;
  return parseCookies(req.headers.cookie).get(AUTH_COOKIE_NAME) === ACCESS_TOKEN;
}

function setPersistentAuthCookie(res: import("http").ServerResponse) {
  res.setHeader("Set-Cookie", authCookieHeader(ACCESS_TOKEN, AUTH_COOKIE_MAX_AGE_SECONDS));
}

function clearPersistentAuthCookie(res: import("http").ServerResponse) {
  res.setHeader("Set-Cookie", authCookieHeader("", 0));
}

const GRAPH_LIBRARY_DIR = process.env.GRAPH_LIBRARY_DIR || process.env.UNDERSTAND_GRAPH_LIBRARY_DIR;

interface GraphLibraryEntry {
  id: string;
  label: string;
  projectName: string;
  description?: string;
  relativePath: string;
  absolutePath: string;
  nodeCount: number;
  edgeCount: number;
  layerCount: number;
  tourStepCount: number;
  analyzedAt?: string;
  gitCommitHash?: string;
}

function graphLibraryRoot(): string | null {
  if (!GRAPH_LIBRARY_DIR) return null;
  const root = path.resolve(GRAPH_LIBRARY_DIR);
  if (!fs.existsSync(root) || !fs.statSync(root).isDirectory()) return null;
  return root;
}

function graphIdForRelativePath(relativePath: string): string {
  return relativePath
    .replace(/\.json$/i, "")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/[.]+/g, "_")
    .replace(/^-+|-+$/g, "");
}

function isPrimaryKnowledgeGraphFile(filePath: string): boolean {
  const base = path.basename(filePath);
  const normalized = filePath.split(path.sep).join("/");
  if (normalized.includes("/run_artifacts/") || normalized.includes("/intermediate/")) return false;
  return base === "knowledge-graph.json" || /_knowledge_graph_understand_anything_\d{4}-\d{2}-\d{2}\.json$/.test(base);
}

function discoverGraphLibrary(): GraphLibraryEntry[] {
  const root = graphLibraryRoot();
  if (!root) return [];
  const entries: GraphLibraryEntry[] = [];
  const stack: Array<{ dir: string; depth: number }> = [{ dir: root, depth: 0 }];
  while (stack.length > 0) {
    const { dir, depth } = stack.pop()!;
    if (depth > 4) continue;
    let dirents: fs.Dirent[];
    try {
      dirents = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const dirent of dirents) {
      const absolutePath = path.join(dir, dirent.name);
      if (dirent.isDirectory()) {
        if (dirent.name === "run_artifacts" || dirent.name === "intermediate" || dirent.name === "tmp") continue;
        stack.push({ dir: absolutePath, depth: depth + 1 });
        continue;
      }
      if (!dirent.isFile() || !isPrimaryKnowledgeGraphFile(absolutePath)) continue;
      try {
        const graph = JSON.parse(fs.readFileSync(absolutePath, "utf-8")) as {
          project?: Record<string, unknown>;
          nodes?: unknown[];
          edges?: unknown[];
          layers?: unknown[];
          tour?: unknown[];
        };
        const relativePath = path.relative(root, absolutePath).split(path.sep).join("/");
        const projectName =
          typeof graph.project?.name === "string" && graph.project.name.trim()
            ? graph.project.name.trim()
            : path.basename(path.dirname(absolutePath));
        entries.push({
          id: graphIdForRelativePath(relativePath),
          label: `${projectName} (${relativePath.split("/")[0]})`,
          projectName,
          description: typeof graph.project?.description === "string" ? graph.project.description : undefined,
          relativePath,
          absolutePath,
          nodeCount: Array.isArray(graph.nodes) ? graph.nodes.length : 0,
          edgeCount: Array.isArray(graph.edges) ? graph.edges.length : 0,
          layerCount: Array.isArray(graph.layers) ? graph.layers.length : 0,
          tourStepCount: Array.isArray(graph.tour) ? graph.tour.length : 0,
          analyzedAt: typeof graph.project?.analyzedAt === "string" ? graph.project.analyzedAt : undefined,
          gitCommitHash: typeof graph.project?.gitCommitHash === "string" ? graph.project.gitCommitHash : undefined,
        });
      } catch (err) {
        console.warn("[understand-anything] Skipping unreadable graph library file:", absolutePath, err);
      }
    }
  }
  return entries.sort((a, b) => a.label.localeCompare(b.label));
}

function publicGraphLibraryPayload() {
  return {
    graphs: discoverGraphLibrary().map(({ absolutePath: _absolutePath, ...entry }) => entry),
  };
}

function graphFileFromLibrary(graphId: string | null): string | null {
  const entries = discoverGraphLibrary();
  if (entries.length === 0) return null;
  if (graphId) {
    return entries.find((entry) => entry.id === graphId)?.absolutePath ?? null;
  }
  return entries[0].absolutePath;
}

function findSelectedKnowledgeGraphFile(url: URL): string | null {
  const requestedGraphId = url.searchParams.get("graph") || process.env.UNDERSTAND_DEFAULT_GRAPH_ID || null;
  return graphFileFromLibrary(requestedGraphId) ?? findGraphFile("knowledge-graph.json");
}

function isPathInside(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function sourceRootFromRunManifest(graphFile: string): string | null {
  const libraryRoot = graphLibraryRoot();
  const absoluteGraphFile = path.resolve(graphFile);
  if (!libraryRoot || !isPathInside(libraryRoot, absoluteGraphFile)) return null;

  const manifestPath = path.join(path.dirname(absoluteGraphFile), "MANIFEST.md");
  if (!fs.existsSync(manifestPath)) return null;

  try {
    const manifest = fs.readFileSync(manifestPath, "utf-8");
    const match =
      manifest.match(/^- Source repo(?: path)?: `([^`]+)`/m) ??
      manifest.match(/^- Source absolute path: `([^`]+)`/m) ??
      manifest.match(/^- Source path: `([^`]+)`/m);
    if (!match) return null;
    const sourceRoot = path.resolve(match[1]);
    if (!fs.existsSync(sourceRoot) || !fs.statSync(sourceRoot).isDirectory()) return null;
    return sourceRoot;
  } catch {
    return null;
  }
}

function sourceRootForGraphFile(graphFile: string): string {
  return sourceRootFromRunManifest(graphFile) ?? projectRootFromGraphFile(graphFile);
}

function graphFileCandidates(fileName: string): string[] {
  const graphDir = process.env.GRAPH_DIR;
  return [
    ...(graphDir
      ? [path.resolve(graphDir, `.understand-anything/${fileName}`)]
      : []),
    path.resolve(process.cwd(), `.understand-anything/${fileName}`),
    path.resolve(process.cwd(), `../../../.understand-anything/${fileName}`),
  ];
}

function findGraphFile(fileName: string): string | null {
  return graphFileCandidates(fileName).find((candidate) => fs.existsSync(candidate)) ?? null;
}

function projectRootFromGraphFile(candidate: string): string {
  return path.dirname(path.dirname(candidate));
}

function normalizeGraphPath(filePath: string, projectRoot: string): string | null {
  const rawPath = path.isAbsolute(filePath)
    ? filePath.startsWith(projectRoot)
      ? path.relative(projectRoot, filePath)
      : null
    : filePath;
  if (rawPath === null) return null;
  const normalized = path.normalize(rawPath);
  if (
    !normalized ||
    normalized === "." ||
    normalized.includes("\0") ||
    normalized === ".." ||
    normalized.startsWith(`..${path.sep}`) ||
    path.isAbsolute(normalized)
  ) {
    return null;
  }
  return normalized.split(path.sep).join("/");
}

function graphFilePathSet(graphFile: string, projectRoot: string): Set<string> {
  const allowed = new Set<string>();
  try {
    const raw = JSON.parse(fs.readFileSync(graphFile, "utf-8")) as {
      nodes?: Array<Record<string, unknown>>;
    };
    for (const node of raw.nodes ?? []) {
      if (typeof node.filePath !== "string") continue;
      const normalized = normalizeGraphPath(node.filePath, projectRoot);
      if (normalized) allowed.add(normalized);
    }
  } catch {
    return allowed;
  }
  return allowed;
}

function detectLanguage(filePath: string): string {
  const ext = path.extname(filePath).slice(1).toLowerCase();
  const byExt: Record<string, string> = {
    bash: "bash",
    c: "c",
    cc: "cpp",
    cpp: "cpp",
    cs: "csharp",
    css: "css",
    go: "go",
    h: "c",
    hpp: "cpp",
    html: "markup",
    java: "java",
    js: "javascript",
    jsx: "jsx",
    json: "json",
    md: "markdown",
    mjs: "javascript",
    py: "python",
    rb: "ruby",
    rs: "rust",
    sh: "bash",
    ts: "typescript",
    tsx: "tsx",
    txt: "text",
    yaml: "yaml",
    yml: "yaml",
  };
  return byExt[ext] ?? "text";
}

function sendJson(res: import("http").ServerResponse, statusCode: number, payload: unknown) {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

function rejectFileRequest(message: string, statusCode = 400) {
  return { statusCode, payload: { error: message } };
}

function readSourceFile(url: URL) {
  const requestedPath = url.searchParams.get("path") ?? "";
  if (!requestedPath) return rejectFileRequest("Missing path");
  if (requestedPath.includes("\0")) return rejectFileRequest("Invalid path");
  if (path.isAbsolute(requestedPath)) return rejectFileRequest("Absolute paths are not allowed");

  const normalizedPath = path.normalize(requestedPath);
  if (
    normalizedPath === "." ||
    normalizedPath.startsWith(`..${path.sep}`) ||
    normalizedPath === ".." ||
    path.isAbsolute(normalizedPath)
  ) {
    return rejectFileRequest("Path must stay inside the project");
  }

  const graphFile = findSelectedKnowledgeGraphFile(url);
  if (!graphFile) {
    return rejectFileRequest("No knowledge graph found. Run /understand first.", 404);
  }

  const projectRoot = sourceRootForGraphFile(graphFile);
  const absoluteFile = path.resolve(projectRoot, normalizedPath);
  const relativeToRoot = path.relative(projectRoot, absoluteFile);
  if (
    !relativeToRoot ||
    relativeToRoot.startsWith(`..${path.sep}`) ||
    relativeToRoot === ".." ||
    path.isAbsolute(relativeToRoot)
  ) {
    return rejectFileRequest("Path must stay inside the project");
  }
  const safeRelativePath = relativeToRoot.split(path.sep).join("/");
  if (!graphFilePathSet(graphFile, projectRoot).has(safeRelativePath)) {
    return rejectFileRequest("File is not in the knowledge graph", 404);
  }

  let stat: fs.Stats;
  try {
    stat = fs.statSync(absoluteFile);
  } catch {
    return rejectFileRequest("File not found", 404);
  }

  if (!stat.isFile()) return rejectFileRequest("Path is not a file");
  if (stat.size > MAX_SOURCE_FILE_BYTES) {
    return rejectFileRequest("File is too large to preview", 413);
  }

  const buffer = fs.readFileSync(absoluteFile);
  if (buffer.includes(0)) return rejectFileRequest("Binary files cannot be previewed", 415);

  const content = buffer.toString("utf8");
  return {
    statusCode: 200,
    payload: {
      path: safeRelativePath,
      language: detectLanguage(relativeToRoot),
      content,
      sizeBytes: buffer.byteLength,
      lineCount: content.length === 0 ? 0 : content.split(/\r\n|\n|\r/).length,
    },
  };
}

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/__tests__/**/*.test.ts"],
  },

  // FIX 1 — bind only to localhost, not 0.0.0.0
  // This blocks direct access from other devices on the same LAN / WiFi.
  // Tailscale Serve can still proxy to this loopback listener; set
  // UNDERSTAND_ALLOWED_HOSTS=<magicdns-name> so Vite accepts the tailnet Host header.
  server: {
    host: "127.0.0.1",
    port: 5173,
    open: `/?token=${ACCESS_TOKEN}`,
    allowedHosts: ["127.0.0.1", "localhost", ...ADDITIONAL_ALLOWED_HOSTS],
  },

  resolve: {
    alias: {
      "@understand-anything/core/schema": path.resolve(__dirname, "../core/dist/schema.js"),
      "@understand-anything/core/search": path.resolve(__dirname, "../core/dist/search.js"),
      "@understand-anything/core/types": path.resolve(__dirname, "../core/dist/types.js"),
    },
  },

  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (/[\\/]node_modules[\\/](react|react-dom|scheduler)[\\/]/.test(id)) {
            return "react-vendor";
          }
          if (id.includes("node_modules/@xyflow/")) return "xyflow";
          // ELK is ~1.6MB raw — split into its own chunk so it doesn't
          // bloat the main bundle. graphology is similarly large.
          if (id.includes("node_modules/elkjs/")) return "elk";
          if (id.includes("node_modules/graphology")) return "graphology";
          if (
            id.includes("node_modules/@dagrejs/") ||
            id.includes("node_modules/d3-force/")
          ) {
            return "graph-layout";
          }
          if (
            id.includes("node_modules/react-markdown/") ||
            id.includes("node_modules/hast-util-to-jsx-runtime/") ||
            /[\\/]node_modules[\\/](remark|rehype|mdast|hast|unist|micromark|decode-named-character-reference|property-information|space-separated-tokens|comma-separated-tokens|html-url-attributes|devlop|bail|ccount|character-entities|is-plain-obj|trim-lines|trough|unified|vfile|zwitch)/.test(id)
          ) {
            return "markdown";
          }
        },
      },
    },
  },

  plugins: [
    react(),
    tailwindcss(),
    {
      name: "serve-knowledge-graph",
      configureServer(server) {
        // Print the access URL once so the developer can open it. LaunchAgent-style
        // deployments can redact the bearer token and expose it through a local
        // mode-0600 token file instead.
        server.httpServer?.once("listening", () => {
          const address = server.httpServer?.address();
          const port = typeof address === "object" && address ? address.port : 5173;
          const tokenForLog = process.env.UNDERSTAND_REDACT_TOKEN_LOG === "true" ? "<redacted>" : ACCESS_TOKEN;
          console.log(`\n  🔑  Dashboard URL: http://127.0.0.1:${port}/?token=${tokenForLog}\n`);
        });

        server.middlewares.use((req, res, next) => {
          const url = new URL(req.url ?? "/", "http://127.0.0.1:5173");
          const pathname = url.pathname;
          const isAuthSessionEndpoint = pathname === "/auth/session";
          const isAuthLogoutEndpoint = pathname === "/auth/logout";
          const isProtectedEndpoint =
            pathname === "/knowledge-graph.json" ||
            pathname === "/domain-graph.json" ||
            pathname === "/diff-overlay.json" ||
            pathname === "/meta.json" ||
            pathname === "/config.json" ||
            pathname === "/file-content.json" ||
            pathname === "/graph-library.json";

          if (isAuthLogoutEndpoint) {
            clearPersistentAuthCookie(res);
            sendJson(res, 200, { ok: true });
            return;
          }

          if (isAuthSessionEndpoint) {
            if (hasValidAccess(url, req)) {
              if (url.searchParams.get("token") === ACCESS_TOKEN) {
                setPersistentAuthCookie(res);
              }
              sendJson(res, 200, { ok: true });
            } else {
              sendJson(res, 403, { error: "Forbidden: missing or invalid token" });
            }
            return;
          }

          if (!isProtectedEndpoint) {
            next();
            return;
          }

          // Require either a matching ?token= or the persistent HttpOnly auth cookie.
          if (!hasValidAccess(url, req)) {
            sendJson(res, 403, { error: "Forbidden: missing or invalid token" });
            return;
          }
          if (url.searchParams.get("token") === ACCESS_TOKEN) {
            setPersistentAuthCookie(res);
          }

          if (pathname === "/graph-library.json") {
            sendJson(res, 200, publicGraphLibraryPayload());
            return;
          }

          if (pathname === "/file-content.json") {
            const result = readSourceFile(url);
            sendJson(res, result.statusCode, result.payload);
            return;
          }

          if (pathname === "/config.json") {
            const configCandidates = graphFileCandidates("config.json");
            for (const candidate of configCandidates) {
              if (fs.existsSync(candidate)) {
                try {
                  const raw = JSON.parse(fs.readFileSync(candidate, "utf-8"));
                  sendJson(res, 200, raw);
                  return;
                } catch {
                  sendJson(res, 500, { error: "Failed to read config file" });
                  return;
                }
              }
            }
            sendJson(res, 200, { autoUpdate: false, outputLanguage: "en" });
            return;
          }

          const fileName =
            pathname === "/diff-overlay.json"
              ? "diff-overlay.json"
              : pathname === "/meta.json"
              ? "meta.json"
              : pathname === "/domain-graph.json"
              ? "domain-graph.json"
              : "knowledge-graph.json";

          const candidates = fileName === "knowledge-graph.json"
            ? [findSelectedKnowledgeGraphFile(url)].filter((candidate): candidate is string => Boolean(candidate))
            : graphFileCandidates(fileName);

          for (const candidate of candidates) {
            if (!fs.existsSync(candidate)) continue;

            // FIX 2 — sanitise absolute file paths before sending the JSON.
            // Nodes can contain filePath values like /Users/alice/company/src/auth.ts.
            // We convert those to relative paths (src/auth.ts) so the developer's
            // home directory and company directory layout are not leaked.
            try {
              const raw = JSON.parse(fs.readFileSync(candidate, "utf-8")) as {
                nodes?: Array<Record<string, unknown>>;
                [key: string]: unknown;
              };

              // Derive the source root from the graph file. Preserved graph-library
              // snapshots may live in Atrium while their readable source files remain
              // in the original checkout recorded by MANIFEST.md.
              const projectRoot = sourceRootForGraphFile(candidate);

              if (Array.isArray(raw.nodes)) {
                raw.nodes = raw.nodes.map((node) => {
                  if (typeof node.filePath !== "string") return node;
                  const abs = node.filePath;
                  // Only relativise paths that actually sit inside projectRoot.
                  // Leave external or already-relative paths untouched.
                  const rel = abs.startsWith(projectRoot)
                    ? abs.slice(projectRoot.length).replace(/^[\\/]/, "")
                    : path.isAbsolute(abs)
                    ? path.basename(abs) // absolute but outside root — use filename only
                    : abs;              // already relative — keep as-is
                  return { ...node, filePath: rel };
                });
              }

              res.setHeader("Content-Type", "application/json");
              res.end(JSON.stringify(raw));
            } catch (err) {
              // If we cannot parse or sanitise the file, refuse to serve it
              // rather than accidentally leaking raw content.
              console.error("[understand-anything] Failed to sanitise graph file:", err);
              res.statusCode = 500;
              res.setHeader("Content-Type", "application/json");
              res.end(JSON.stringify({ error: "Failed to read graph file" }));
            }
            return;
          }

          // No matching file found on disk.
          res.statusCode = 404;
          if (pathname === "/knowledge-graph.json") {
            res.setHeader("Content-Type", "application/json");
            res.end(JSON.stringify({ error: "No knowledge graph found. Run /understand first." }));
          } else {
            res.end();
          }
        });
      },
    },
  ],
});
