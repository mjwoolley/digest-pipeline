export function FunnelChart({ funnel }) {
  if (!funnel) return null;

  const steps = [
    { key: 'extracted', label: 'Extracted' },
    { key: 'clustered', label: 'Clustered' },
    { key: 'deduped', label: 'Deduped' },
    { key: 'prioritized', label: 'Prioritized' },
    { key: 'formatted', label: 'Formatted' },
  ];

  const max = Math.max(...steps.map((s) => funnel[s.key] || 0), 1);

  return (
    <div class="funnel">
      {steps.map(({ key, label }) => {
        const value = funnel[key] || 0;
        const pct = (value / max) * 100;
        return (
          <div key={key} class="funnel-row">
            <span class="funnel-label">{label}</span>
            <div class="funnel-bar-track">
              <div
                class="funnel-bar-fill"
                style={`width: ${pct}%`}
              />
            </div>
            <span class="funnel-value">{value}</span>
          </div>
        );
      })}
    </div>
  );
}
