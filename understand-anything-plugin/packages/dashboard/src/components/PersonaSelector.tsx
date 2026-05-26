import { useDashboardStore } from "../store";
import { useI18n } from "../contexts/I18nContext";
import type { DashboardMode } from "../store";

export default function PersonaSelector() {
  const dashboardMode = useDashboardStore((s) => s.dashboardMode);
  const setDashboardMode = useDashboardStore((s) => s.setDashboardMode);
  const { t } = useI18n();

  const modes: { id: DashboardMode; label: string; description: string }[] = [
    {
      id: "overview",
      label: t.personaSelector.overview,
      description: t.personaSelector.overviewDesc,
    },
    {
      id: "learn",
      label: t.personaSelector.learn,
      description: t.personaSelector.learnDesc,
    },
    {
      id: "deep-dive",
      label: t.personaSelector.deepDive,
      description: t.personaSelector.deepDiveDesc,
    },
  ];

  return (
    <div className="flex items-center gap-1 bg-elevated rounded-lg p-0.5" aria-label="Dashboard mode">
      {modes.map((mode) => (
        <button
          key={mode.id}
          onClick={() => setDashboardMode(mode.id)}
          title={mode.description}
          aria-pressed={dashboardMode === mode.id}
          className={`px-2.5 py-1 rounded text-[11px] font-medium transition-colors ${
            dashboardMode === mode.id
              ? "bg-accent/20 text-accent"
              : "text-text-muted hover:text-text-secondary hover:bg-surface"
          }`}
        >
          {mode.label}
        </button>
      ))}
    </div>
  );
}
