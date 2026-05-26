import { useMemo } from "react";
import { useDashboardStore } from "../store";

export default function DeepDivePanel() {
  const graph = useDashboardStore((s) => s.graph);
  const selectedNodeId = useDashboardStore((s) => s.selectedNodeId);
  const navigateToNode = useDashboardStore((s) => s.navigateToNode);
  const setFocusNode = useDashboardStore((s) => s.setFocusNode);
  const openCodeViewer = useDashboardStore((s) => s.openCodeViewer);

  const model = useMemo(() => {
    if (!graph) return null;
    const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
    const selected = selectedNodeId ? nodesById.get(selectedNodeId) ?? null : null;
    const edges = selectedNodeId
      ? graph.edges.filter((edge) => edge.source === selectedNodeId || edge.target === selectedNodeId)
      : graph.edges;
    const implementationAnchors = graph.nodes
      .filter((node) => node.type === "function" || node.type === "class")
      .sort((a, b) => {
        const aDegree = graph.edges.filter((edge) => edge.source === a.id || edge.target === a.id).length;
        const bDegree = graph.edges.filter((edge) => edge.source === b.id || edge.target === b.id).length;
        return bDegree - aDegree || a.name.localeCompare(b.name);
      })
      .slice(0, 8);
    const connectedNodes = selectedNodeId
      ? edges
          .map((edge) => edge.source === selectedNodeId ? edge.target : edge.source)
          .filter((id, index, arr) => arr.indexOf(id) === index)
          .map((id) => nodesById.get(id))
          .filter(Boolean)
          .slice(0, 10)
      : [];
    return { selected, edges: edges.slice(0, 12), connectedNodes, implementationAnchors };
  }, [graph, selectedNodeId]);

  if (!graph || !model) return null;

  return (
    <div className="p-4 border-t border-border-subtle bg-root/40" data-panel="deep-dive">
      <div className="mb-4">
        <div className="text-[10px] font-semibold uppercase tracking-[0.18em] text-accent mb-1">Deep Dive</div>
        <h2 className="font-heading text-lg text-text-primary">
          {model.selected ? model.selected.name : "Implementation inspection"}
        </h2>
        <p className="text-xs text-text-muted mt-1 leading-relaxed">
          Dependency edges, source anchors, and implementation-level nodes for close inspection.
        </p>
      </div>

      {model.selected && (
        <div className="mb-4 flex gap-2">
          <button
            type="button"
            onClick={() => setFocusNode(model.selected!.id)}
            className="px-2.5 py-1 rounded bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25"
          >
            Focus neighborhood
          </button>
          {model.selected.filePath && (
            <button
              type="button"
              onClick={() => openCodeViewer(model.selected!.id)}
              className="px-2.5 py-1 rounded bg-elevated text-text-secondary text-xs font-medium hover:text-text-primary"
            >
              Open source
            </button>
          )}
        </div>
      )}

      {model.connectedNodes.length > 0 && (
        <section className="mb-4">
          <h3 className="text-[11px] font-semibold uppercase tracking-wider text-text-muted mb-2">Connected nodes</h3>
          <div className="space-y-1.5">
            {model.connectedNodes.map((node) => node && (
              <button key={node.id} type="button" onClick={() => navigateToNode(node.id)} className="w-full text-left rounded border border-border-subtle bg-surface/60 hover:bg-elevated px-2.5 py-2">
                <div className="text-xs font-medium text-text-primary truncate">{node.name}</div>
                <div className="text-[10px] text-text-muted truncate">{node.type} · {node.summary}</div>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="mb-4">
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-text-muted mb-2">Implementation anchors</h3>
        <div className="space-y-1.5">
          {model.implementationAnchors.map((node) => (
            <button key={node.id} type="button" onClick={() => navigateToNode(node.id)} className="w-full text-left rounded border border-border-subtle bg-surface/60 hover:bg-elevated px-2.5 py-2">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-text-primary truncate">{node.name}</span>
                <span className="ml-auto text-[9px] text-accent uppercase">{node.type}</span>
              </div>
              <div className="text-[10px] text-text-muted truncate">{node.filePath}{node.lineRange ? `:${node.lineRange[0]}-${node.lineRange[1]}` : ""}</div>
            </button>
          ))}
        </div>
      </section>

      <section>
        <h3 className="text-[11px] font-semibold uppercase tracking-wider text-text-muted mb-2">Relationship sample</h3>
        <div className="space-y-1 font-mono text-[10px] text-text-muted">
          {model.edges.map((edge, index) => (
            <div key={`${edge.source}-${edge.target}-${edge.type}-${index}`} className="rounded bg-surface/60 border border-border-subtle px-2 py-1 truncate">
              {edge.source} → {edge.type} → {edge.target}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
