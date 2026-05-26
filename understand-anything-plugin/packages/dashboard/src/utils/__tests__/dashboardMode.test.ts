import { describe, expect, it, beforeEach } from "vitest";
import { useDashboardStore } from "../../store";
import type { KnowledgeGraph } from "@understand-anything/core/types";

const graph: KnowledgeGraph = {
  version: "1.0.0",
  kind: "codebase",
  project: {
    name: "sample",
    languages: ["python"],
    frameworks: [],
    description: "sample graph",
    analyzedAt: "2026-05-26T00:00:00Z",
    gitCommitHash: "abc123",
  },
  nodes: [
    { id: "document:README.md", type: "document", name: "README.md", filePath: "README.md", summary: "docs", tags: ["docs"], complexity: "simple" },
    { id: "file:src/main.py", type: "file", name: "main.py", filePath: "src/main.py", summary: "entry", tags: ["python"], complexity: "moderate" },
    { id: "function:src/main.py:run", type: "function", name: "run", filePath: "src/main.py", lineRange: [1, 20], summary: "runs", tags: ["function"], complexity: "moderate" },
  ],
  edges: [
    { source: "file:src/main.py", target: "function:src/main.py:run", type: "contains", direction: "forward", weight: 0.95 },
  ],
  layers: [
    { id: "layer:docs", name: "Docs", description: "documentation", nodeIds: ["document:README.md"] },
    { id: "layer:core", name: "Core", description: "core code", nodeIds: ["file:src/main.py"] },
  ],
  tour: [
    { order: 1, title: "Project Orientation", description: "Read the docs.", nodeIds: ["document:README.md"] },
    { order: 2, title: "Entrypoint", description: "Read the entrypoint.", nodeIds: ["file:src/main.py"], languageLesson: "Python modules expose importable functions." },
  ],
};

describe("dashboard mode state", () => {
  beforeEach(() => {
    useDashboardStore.getState().stopTour();
    useDashboardStore.getState().setDashboardMode("overview");
    useDashboardStore.getState().setGraph(graph);
  });

  it("keeps Overview as a high-level mode, not Learn by default", () => {
    const state = useDashboardStore.getState();
    expect(state.dashboardMode).toBe("overview");
    expect(state.persona).toBe("non-technical");
    expect(state.detailLevel).toBe("file");
  });

  it("maps Learn to the tutorial/code-reading surface", () => {
    useDashboardStore.getState().setDashboardMode("learn");
    const state = useDashboardStore.getState();
    expect(state.dashboardMode).toBe("learn");
    expect(state.persona).toBe("junior");
    expect(state.detailLevel).toBe("class");
  });

  it("maps Deep Dive to implementation-level inspection", () => {
    useDashboardStore.getState().startTour();
    useDashboardStore.getState().setDashboardMode("deep-dive");
    const state = useDashboardStore.getState();
    expect(state.dashboardMode).toBe("deep-dive");
    expect(state.persona).toBe("experienced");
    expect(state.detailLevel).toBe("class");
    expect(state.showFunctionsInClassView).toBe(true);
    expect(state.tourActive).toBe(false);
  });

  it("starting the guided tour forces Learn mode so the tutorial is visible", () => {
    useDashboardStore.getState().startTour();
    const state = useDashboardStore.getState();
    expect(state.dashboardMode).toBe("learn");
    expect(state.persona).toBe("junior");
    expect(state.tourActive).toBe(true);
    expect(state.tourHighlightedNodeIds).toEqual(["document:README.md"]);
  });
});
