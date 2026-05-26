export interface GraphLibraryEntry {
  id: string;
  label: string;
  projectName: string;
  description?: string;
  relativePath: string;
  nodeCount: number;
  edgeCount: number;
  layerCount: number;
  tourStepCount: number;
  analyzedAt?: string;
  gitCommitHash?: string;
}

interface GraphLibrarySelectorProps {
  entries: GraphLibraryEntry[];
  activeGraphId: string | null;
  onSelect: (graphId: string) => void;
}

export default function GraphLibrarySelector({
  entries,
  activeGraphId,
  onSelect,
}: GraphLibrarySelectorProps) {
  if (entries.length === 0) return null;

  const activeEntry = entries.find((entry) => entry.id === activeGraphId) ?? entries[0];

  return (
    <div className="flex items-center gap-2 min-w-0">
      <label
        htmlFor="graph-library-selector"
        className="text-[10px] font-semibold uppercase tracking-wider text-text-muted whitespace-nowrap"
      >
        Graph
      </label>
      <select
        id="graph-library-selector"
        value={activeEntry.id}
        onChange={(event) => onSelect(event.target.value)}
        className="min-w-0 flex-1 max-w-[260px] lg:max-w-[360px] bg-elevated border border-border-subtle rounded-lg px-2.5 py-1.5 text-xs text-text-primary focus:outline-none focus:border-accent"
        title={activeEntry.relativePath}
      >
        {entries.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.label}
          </option>
        ))}
      </select>
      <span
        className="hidden xl:inline text-[10px] text-text-muted whitespace-nowrap"
        title={activeEntry.description ?? activeEntry.relativePath}
      >
        {activeEntry.nodeCount.toLocaleString()} nodes · {activeEntry.edgeCount.toLocaleString()} edges
      </span>
    </div>
  );
}
