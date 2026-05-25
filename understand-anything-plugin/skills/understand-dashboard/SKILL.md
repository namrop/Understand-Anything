---
name: understand-dashboard
description: Launch the interactive web dashboard to visualize a codebase's knowledge graph
argument-hint: [project-path]
---

# /understand-dashboard

Start the Understand Anything dashboard to visualize the knowledge graph for the current project.

## Instructions

1. Determine the project directory:
   - If `$ARGUMENTS` contains a path, use that as the project directory
   - Otherwise, use the current working directory

2. Check that `.understand-anything/knowledge-graph.json` exists in the project directory. If not, tell the user:
   ```
   No knowledge graph found. Run /understand first to analyze this project.
   ```

3. Find the dashboard code. The dashboard is at `packages/dashboard/` relative to this plugin's root directory. Check these paths in order and use the first that exists:
   - `${CLAUDE_PLUGIN_ROOT}/packages/dashboard/` (Claude Code runtime root, highest priority)
   - `~/.understand-anything-plugin/packages/dashboard/` (universal symlink, all installs)
   - Two levels up from `~/.agents/skills/understand-dashboard` real path (self-relative fallback)
   - Two levels up from `~/.copilot/skills/understand-dashboard` real path (Copilot personal skills fallback)
   - Common clone-based install roots:
     - `~/.codex/understand-anything/understand-anything-plugin/packages/dashboard/`
     - `~/.opencode/understand-anything/understand-anything-plugin/packages/dashboard/`
     - `~/.pi/understand-anything/understand-anything-plugin/packages/dashboard/`
     - `~/understand-anything/understand-anything-plugin/packages/dashboard/`

   Use the Bash tool to resolve:
   ```bash
   SKILL_REAL=$(realpath ~/.agents/skills/understand-dashboard 2>/dev/null || readlink -f ~/.agents/skills/understand-dashboard 2>/dev/null || echo "")
   SELF_RELATIVE=$([ -n "$SKILL_REAL" ] && cd "$SKILL_REAL/../.." 2>/dev/null && pwd || echo "")
   COPILOT_SKILL_REAL=$(realpath ~/.copilot/skills/understand-dashboard 2>/dev/null || readlink -f ~/.copilot/skills/understand-dashboard 2>/dev/null || echo "")
   COPILOT_SELF_RELATIVE=$([ -n "$COPILOT_SKILL_REAL" ] && cd "$COPILOT_SKILL_REAL/../.." 2>/dev/null && pwd || echo "")

   PLUGIN_ROOT=""
   for candidate in \
     "${CLAUDE_PLUGIN_ROOT}" \
     "$HOME/.understand-anything-plugin" \
     "$SELF_RELATIVE" \
     "$COPILOT_SELF_RELATIVE" \
     "$HOME/.codex/understand-anything/understand-anything-plugin" \
     "$HOME/.opencode/understand-anything/understand-anything-plugin" \
     "$HOME/.pi/understand-anything/understand-anything-plugin" \
     "$HOME/understand-anything/understand-anything-plugin"; do
     if [ -n "$candidate" ] && [ -d "$candidate/packages/dashboard" ]; then
       PLUGIN_ROOT="$candidate"; break
     fi
   done

   if [ -z "$PLUGIN_ROOT" ]; then
     echo "Error: Cannot find the understand-anything plugin root."
     echo "Checked:"
     echo "  - ${CLAUDE_PLUGIN_ROOT:-<unset CLAUDE_PLUGIN_ROOT>}"
     echo "  - $HOME/.understand-anything-plugin"
     echo "  - ${SELF_RELATIVE:-<unresolved path derived from ~/.agents/skills/understand-dashboard>}"
     echo "  - ${COPILOT_SELF_RELATIVE:-<unresolved path derived from ~/.copilot/skills/understand-dashboard>}"
     echo "  - $HOME/.codex/understand-anything/understand-anything-plugin"
     echo "  - $HOME/.opencode/understand-anything/understand-anything-plugin"
     echo "  - $HOME/.pi/understand-anything/understand-anything-plugin"
     echo "  - $HOME/understand-anything/understand-anything-plugin"
     echo "Make sure you followed the installation instructions for your platform."
     exit 1
   fi
   ```

4. Install dependencies and build if needed:
   ```bash
   cd <dashboard-dir> && pnpm install --frozen-lockfile 2>/dev/null || pnpm install
   ```
   Then ensure the core package is built (the dashboard depends on it):
   ```bash
   cd <plugin-root> && pnpm --filter @understand-anything/core build
   ```

5. Start the Vite dev server pointing at the project's knowledge graph. Before launch, discover the MagicDNS host if Tailscale is available and pass it as `UNDERSTAND_ALLOWED_HOSTS`; this lets Vite accept the Host header that Tailscale Serve will use while keeping the actual listener bound to loopback:
   ```bash
   TS_STATUS_JSON=$(mktemp -t ua-ts-status.XXXXXX.json)
   UNDERSTAND_ALLOWED_HOSTS=""
   if command -v tailscale >/dev/null 2>&1 && tailscale status --json > "$TS_STATUS_JSON" 2>/dev/null; then
     UNDERSTAND_ALLOWED_HOSTS=$(python3 - "$TS_STATUS_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
self = d.get('Self', {})
print((self.get('DNSName') or self.get('HostName') or '').rstrip('.'))
PY
)
   fi
   cd <dashboard-dir> && GRAPH_DIR=<project-dir> UNDERSTAND_ALLOWED_HOSTS="$UNDERSTAND_ALLOWED_HOSTS" npx vite --host 127.0.0.1
   ```
   Run this in the background so the user can continue working. Keep Vite bound to loopback; do **not** bind it to `0.0.0.0`.

6. **Capture the access token URL from the server output.** The Vite server prints a line like:
   ```
   🔑  Dashboard URL: http://127.0.0.1:<PORT>/?token=<TOKEN>
   ```
   Extract the full URL including the `?token=` parameter. The token is required to access the knowledge graph data — without it the dashboard will show an "Access Token Required" gate.

7. **Expose the dashboard through Tailscale Serve by default when available.** This Hermes/Lux install should prefer a tailnet HTTPS URL over a raw localhost URL, so Luis can open the dashboard from other tailnet devices without broad LAN/public binding.

   - First parse the Vite port and token from the captured local URL.
   - Discover the node's MagicDNS name with `tailscale status --json`; strip any trailing dot.
   - Use a dedicated HTTPS serve port, defaulting to `${UA_TAILSCALE_HTTPS_PORT:-9444}`. Do **not** overwrite an existing root `:443` Tailscale Serve config; if the chosen port is already present in `tailscale serve status --json`, increment to the next free port up to `9460`.
   - Run:
     ```bash
     tailscale serve --yes --bg --https=<TAILSCALE_HTTPS_PORT> http://127.0.0.1:<VITE_PORT>
     ```
   - Construct and report the tailnet URL:
     ```text
     https://<magicdns-name>:<TAILSCALE_HTTPS_PORT>/?token=<TOKEN>
     ```
   - If Tailscale is unavailable, offline, or `tailscale serve` fails, fall back to the local URL and report the Tailscale failure explicitly. Do not block local dashboard use on Tailscale.

   Reference shell for the Tailscale step:
   ```bash
   LOCAL_URL="http://127.0.0.1:<PORT>/?token=<TOKEN>"
   VITE_PORT=$(printf '%s\n' "$LOCAL_URL" | sed -E 's#^http://127\.0\.0\.1:([0-9]+)/?.*$#\1#')
   TOKEN=$(printf '%s\n' "$LOCAL_URL" | sed -E 's#.*[?&]token=([^&]+).*#\1#')
   TS_STATUS_JSON=$(mktemp -t ua-ts-status.XXXXXX.json)
   TS_SERVE_JSON=$(mktemp -t ua-ts-serve.XXXXXX.json)
   tailscale status --json > "$TS_STATUS_JSON"
   TS_DNS=$(python3 - "$TS_STATUS_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
self = d.get('Self', {})
print((self.get('DNSName') or self.get('HostName') or '').rstrip('.'))
PY
)
   TS_PORT=${UA_TAILSCALE_HTTPS_PORT:-9444}
   while true; do
     tailscale serve status --json > "$TS_SERVE_JSON" 2>/dev/null || printf '{}' > "$TS_SERVE_JSON"
     if ! python3 - "$TS_SERVE_JSON" "$TS_PORT" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
raise SystemExit(0 if sys.argv[2] in (d.get('TCP') or {}) else 1)
PY
     then
       break
     fi
     TS_PORT=$((TS_PORT + 1))
     if [ "$TS_PORT" -gt 9460 ]; then echo "No free Tailscale Serve HTTPS port in 9444-9460" >&2; exit 1; fi
   done
   tailscale serve --yes --bg --https="$TS_PORT" "http://127.0.0.1:$VITE_PORT"
   printf 'https://%s:%s/?token=%s\n' "$TS_DNS" "$TS_PORT" "$TOKEN"
   ```

8. Report to the user, including the full tokenized Tailscale URL when available and the local fallback URL:
   ```
   Dashboard started at https://<magicdns-name>:<TAILSCALE_HTTPS_PORT>/?token=<TOKEN>
   Local fallback: http://127.0.0.1:<PORT>/?token=<TOKEN>
   Viewing: <project-dir>/.understand-anything/knowledge-graph.json

   The dashboard is running in the background. Stop the Vite process to stop the local server. The Tailscale Serve route is persistent; remove the dedicated route with `tailscale serve --yes --https=<TAILSCALE_HTTPS_PORT> off` when you no longer want it.
   ```
   **Important:** Always include the `?token=` parameter in every URL you share. If you omit it, the user will be blocked by the token gate and have to manually find the token in the terminal output.

## Notes

- The dashboard remains loopback-bound locally, then is exposed to the tailnet with Tailscale Serve on a dedicated HTTPS port (default first choice: `9444`)
- If port 5173 is already in use, Vite will pick the next available port
- The `GRAPH_DIR` environment variable tells the dashboard where to find the knowledge graph
- Tailscale Serve routes are persistent; prefer dedicated non-443 ports here so existing root/path serve config is not clobbered. Clean a dashboard route with `tailscale serve --yes --https=<port> off`, not `tailscale serve reset`.
