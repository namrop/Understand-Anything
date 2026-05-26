import { describe, expect, it } from "vitest";
import type { KnowledgeGraph } from "@understand-anything/core/types";
import { buildKnowledgeGraphViewport } from "../knowledgeGraphViewport";

function makeGraph(nodeCount: number): KnowledgeGraph {
  const nodes = Array.from({ length: nodeCount }, (_, i) => ({
    id: `node:${i}`,
    type: "article" as const,
    name: `Node ${i}`,
    summary: `Node ${i} summary`,
    tags: ["test"],
    complexity: "simple" as const,
  }));

  const edges = Array.from({ length: Math.max(0, nodeCount - 1) }, (_, i) => ({
    source: `node:${i}`,
    target: `node:${i + 1}`,
    type: "related" as const,
    direction: "forward" as const,
    weight: 0.5,
  }));

  return {
    version: "1.0.0",
    kind: "knowledge",
    project: {
      name: "Large graph",
      languages: ["markdown"],
      frameworks: [],
      description: "A large knowledge graph",
      analyzedAt: "2026-05-26T00:00:00.000Z",
      gitCommitHash: "abc123",
    },
    nodes,
    edges,
    layers: [],
    tour: [],
  };
}

describe("buildKnowledgeGraphViewport", () => {
  it("does not render a large knowledge graph by default", () => {
    const result = buildKnowledgeGraphViewport(makeGraph(6), { maxNodes: 3 });

    expect(result.mode).toBe("overview");
    expect(result.graph).toBeNull();
    expect(result.totalNodes).toBe(6);
    expect(result.renderedNodes).toBe(0);
  });

  it("renders a bounded neighborhood around active seeds for large knowledge graphs", () => {
    const result = buildKnowledgeGraphViewport(makeGraph(6), {
      maxNodes: 3,
      selectedNodeId: "node:2",
    });

    expect(result.mode).toBe("bounded");
    expect(result.graph?.nodes.map((node) => node.id).sort()).toEqual([
      "node:1",
      "node:2",
      "node:3",
    ]);
    expect(result.graph?.edges).toEqual([
      expect.objectContaining({ source: "node:1", target: "node:2" }),
      expect.objectContaining({ source: "node:2", target: "node:3" }),
    ]);
    expect(result.renderedNodes).toBe(3);
  });

  it("renders small knowledge graphs in full", () => {
    const graph = makeGraph(3);
    const result = buildKnowledgeGraphViewport(graph, { maxNodes: 3 });

    expect(result.mode).toBe("full");
    expect(result.graph).toBe(graph);
    expect(result.renderedNodes).toBe(3);
  });
});
