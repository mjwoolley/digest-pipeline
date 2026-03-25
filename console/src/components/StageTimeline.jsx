export function StageTimeline({ stages }) {
  if (!stages || stages.length === 0) {
    return <div class="empty-state">No stage data available.</div>;
  }

  return (
    <div class="timeline">
      {stages.map((stage, i) => {
        const isError = stage.type === 'error';
        const tokens = stage.tokens || {};
        const hasTokens = tokens.input > 0 || tokens.output > 0;

        return (
          <div key={i} class={`timeline-item ${isError ? 'timeline-item--error' : ''}`}>
            <div class="timeline-dot" />
            {i < stages.length - 1 && <div class="timeline-line" />}
            <div class="timeline-content">
              <div class="timeline-header">
                <span class="timeline-stage">{stage.stage}</span>
                <div class="timeline-meta">
                  {stage.duration_s != null && (
                    <span class="timeline-chip">
                      {stage.duration_s >= 1
                        ? `${stage.duration_s.toFixed(1)}s`
                        : `${(stage.duration_s * 1000).toFixed(0)}ms`}
                    </span>
                  )}
                  {hasTokens && (
                    <span class="timeline-chip">
                      {(tokens.input + (tokens.output || 0)).toLocaleString()} tok
                    </span>
                  )}
                  {tokens.cost > 0 && (
                    <span class="timeline-chip">
                      ${tokens.cost < 0.001 ? tokens.cost.toExponential(1) : tokens.cost.toFixed(3)}
                    </span>
                  )}
                </div>
              </div>
              {stage.detail && (
                <pre class="timeline-detail">{stage.detail}</pre>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
