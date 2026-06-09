---
name: understand-graph-library-ops
description: Publish and operate Understand Anything knowledge graphs on Luis's persistent multi-project graph-library dashboard
---

# Understand Graph Library Operations

Use this skill when an Understand Anything graph needs to be preserved, published, repaired, or verified on Luis's persistent multi-project graph-library dashboard, especially on acubens/Caddy/Tailscale.

This is a companion operations skill for the hub-provided `understand` skill. Do not duplicate the core graph-generation workflow here; use this for the publication, dashboard, and long-running-run discipline around completed graphs.

## Core rules

1. **Do not hijack port 5173.** On Luis's acubens setup, `127.0.0.1:5173` belongs to the persistent graph-library LaunchAgent `com.lux.understand-graph-library`. Never replace it with a one-off `GRAPH_DIR=<repo> vite --port 5173` dashboard.
2. **Preserve completed runs in the Atrium graph-library runs directory:**
   `/Users/luisramirez/Digital_Workspace/27_knowledge/understand_anything/artifacts/runs/<run-id>/`
3. **Register runs in the graph-library surfaces:** update the graph index and access/default graph env surfaces rather than serving directly from the source repo.
4. **Verify through the existing service:** check `/graph-library.json` and `/knowledge-graph.json?graph=<graph-id>` on the persistent dashboard.
5. **For large repos, use durable background scripts:** run smoke tests first, then full direct-provider runs with explicit provider/model/fallback behavior and postprocessing.

## Large run pattern

For a large repository:

1. Preflight the source repo: absolute path, branch, HEAD, dirty state, package identity, file count, expected batch count.
2. Run a small smoke test first with the direct provider runner, e.g. `--limit 1`, `--no-resume`, workers `1`.
3. For Luis's preferred Codex-backed high-fidelity runs, use:
   - provider: `codex-cli`
   - model: `gpt-5.5`
   - fallback disabled via `UA_DIRECT_FAIL_ON_FALLBACK=1`
4. Launch the full run through a persistent background script when it may outlive the current context window.
5. If a few batches fail from capacity/timeouts, do **not** discard successful batches. Rerun without `--no-resume`, often with `--workers 1` and longer `--codex-timeout`, so only missing/error batches are regenerated.
6. After completion, copy artifacts to the runs directory, generate/retain `MANIFEST.md`, update index/default graph surfaces, and verify dashboard visibility.

## Publication artifact shape

A preserved run should include:

- `knowledge-graph.json`
- `meta.json`
- `fingerprints.json` when available
- `direct-run-report.json` when using the direct runner
- `MANIFEST.md` containing source repo path, source commit, provider/model, graph id, timestamps, validation summary, and dashboard URLs

## Verification checklist

Before saying the graph is published:

- The direct run report shows expected completed batches, failed `0`, fallback `0`.
- `knowledge-graph.json` parses and has nonzero nodes/edges/layers/tour.
- The persistent graph library's `/graph-library.json` includes the new graph id.
- `/knowledge-graph.json?graph=<graph-id>` returns the new graph.
- The dashboard selector still lists multiple projects, proving the persistent graph-library service was not replaced by a one-off project dashboard.

## Graph Copilot audit/debug operations

The graph-library dashboard has a source-aware Graph Copilot endpoint at `/graph-copilot.json`. On Luis's acubens service, the launch script wires DeepSeek and audit logging:

- Provider env is read from `~/.hermes/.env` (`DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`) and exported as `UNDERSTAND_COPILOT_*`.
- JSONL audit log path defaults to `~/.hermes/logs/understand-graph-copilot.jsonl`.
- `UNDERSTAND_COPILOT_DEBUG_CONTEXT=true` includes the full bounded graph context and source snippets in each audit row for temporary quality debugging.
- Audit rows must never include provider API keys. Verify with an exact-key containment check rather than searching generic substrings such as `sk-`, which may appear inside normal words like `disk-cleanup`.
- The endpoint now performs a small loop: bounded graph retrieval → provider file-selection plan → load bounded source files from graph-known `filePath`s under the manifest source root → final provider answer with chat history and source snippets → JSONL audit row.

## Recovery if port 5173 was hijacked

If a one-off dashboard is occupying 5173:

1. Identify the process serving the one-off dashboard.
2. Kill only that process.
3. Restart the persistent service:

```bash
launchctl kickstart -k gui/$(id -u)/com.lux.understand-graph-library
```

4. Verify that the selector lists multiple projects again.

## Reference files

- `references/luis-graph-library-runbook.md` — condensed session-derived runbook for acubens graph-library publishing and recovery.
