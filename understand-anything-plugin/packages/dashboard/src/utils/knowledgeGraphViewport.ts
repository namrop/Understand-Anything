import type { KnowledgeGraph } from "@understand-anything/core/types";

export const DEFAULT_KNOWLEDGE_GRAPH_RENDER_NODE_LIMIT = 750;
export const DEFAULT_KNOWLEDGE_GRAPH_SEED_LIMIT = 40;

export type KnowledgeGraphViewportMode = "full" | "bounded" | "overview";

export interface KnowledgeGraphViewportOptions {
  maxNodes?: number;
  seedLimit?: number;
  selectedNodeId?: string | null;
  focusNodeId?: string | null;
  searchResultNodeIds?: string[];
  tourHighlightedNodeIds?: string[];
}

export interface KnowledgeGraphViewportResult {
  mode: KnowledgeGraphViewportMode;
  graph: KnowledgeGraph | null;
  totalNodes: number;
  totalEdges: number;
  renderedNodes: number;
  renderedEdges: number;
  seedNodeIds: string[];
  maxNodes: number;
}

function uniqueExistingSeeds(seedCandidates: Array<string | null | undefined>, nodeIds: Set<string>, limit: number): string[] {
  const seeds: string[] = [];
  const seen = new Set<string>();
  for (const candidate of seedCandidates) {
    if (!candidate || seen.has(candidate) || !nodeIds.has(candidate)) continue;
    seen.add(candidate);
    seeds.push(candidate);
    if (seeds.length >= limit) break;
  }
  return seeds;
}

function filterGraphToNodeIds(graph: KnowledgeGraph, nodeIds: Set<string>): KnowledgeGraph {
  const nodes = graph.nodes.filter((node) => nodeIds.has(node.id));
  const edges = graph.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
  const layers = graph.layers
    .map((layer) => ({
      ...layer,
      nodeIds: layer.nodeIds.filter((nodeId) => nodeIds.has(nodeId)),
    }))
    .filter((layer) => layer.nodeIds.length > 0);
  const tour = graph.tour
    .map((step) => ({
      ...step,
      nodeIds: step.nodeIds.filter((nodeId) => nodeIds.has(nodeId)),
    }))
    .filter((step) => step.nodeIds.length > 0);

  return {
    ...graph,
    nodes,
    edges,
    layers,
    tour,
  };
}

/**
 * Build the renderable viewport for a knowledge graph.
 *
 * Large Atrium-scale graphs cannot be force-laid-out synchronously in the
 * browser. For those graphs, default to an overview/search state and only
 * return a bounded one-hop neighborhood once search, tour, or selection gives
 * the renderer concrete seeds.
 */
export function buildKnowledgeGraphViewport(
  graph: KnowledgeGraph,
  options: KnowledgeGraphViewportOptions = {},
): KnowledgeGraphViewportResult {
  const maxNodes = options.maxNodes ?? DEFAULT_KNOWLEDGE_GRAPH_RENDER_NODE_LIMIT;
  const seedLimit = options.seedLimit ?? DEFAULT_KNOWLEDGE_GRAPH_SEED_LIMIT;
  const totalNodes = graph.nodes.length;
  const totalEdges = graph.edges.length;

  if (graph.kind !== "knowledge" || totalNodes <= maxNodes) {
    return {
      mode: "full",
      graph,
      totalNodes,
      totalEdges,
      renderedNodes: totalNodes,
      renderedEdges: totalEdges,
      seedNodeIds: [],
      maxNodes,
    };
  }

  const nodeIds = new Set<string>(graph.nodes.map((node) => node.id));
  const seedNodeIds = uniqueExistingSeeds(
    [
      options.focusNodeId,
      options.selectedNodeId,
      ...(options.tourHighlightedNodeIds ?? []),
      ...(options.searchResultNodeIds ?? []),
    ],
    nodeIds,
    seedLimit,
  );

  if (seedNodeIds.length === 0) {
    return {
      mode: "overview",
      graph: null,
      totalNodes,
      totalEdges,
      renderedNodes: 0,
      renderedEdges: 0,
      seedNodeIds,
      maxNodes,
    };
  }

  const included = new Set<string>();
  const seedSet = new Set(seedNodeIds);
  for (const seed of seedNodeIds) {
    included.add(seed);
    if (included.size >= maxNodes) break;
  }

  for (const edge of graph.edges) {
    if (included.size >= maxNodes) break;
    const sourceIsSeed = seedSet.has(edge.source);
    const targetIsSeed = seedSet.has(edge.target);
    if (!sourceIsSeed && !targetIsSeed) continue;
    if (sourceIsSeed && nodeIds.has(edge.target)) included.add(edge.target);
    if (included.size >= maxNodes) break;
    if (targetIsSeed && nodeIds.has(edge.source)) included.add(edge.source);
  }

  const viewportGraph = filterGraphToNodeIds(graph, included);
  return {
    mode: "bounded",
    graph: viewportGraph,
    totalNodes,
    totalEdges,
    renderedNodes: viewportGraph.nodes.length,
    renderedEdges: viewportGraph.edges.length,
    seedNodeIds,
    maxNodes,
  };
}
