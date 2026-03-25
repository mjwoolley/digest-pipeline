import { useApi } from '../hooks/useApi';
import { StatusBadge } from '../components/StatusBadge';
import { StageTimeline } from '../components/StageTimeline';
import { FunnelChart } from '../components/FunnelChart';

export function RunDetail({ slug, date, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/runs/${date}`, refreshInterval);
  const { data: funnel } = useApi(`/api/digests/${slug}/runs/${date}/funnel`, refreshInterval);
  const { data: sources } = useApi(`/api/digests/${slug}/runs/${date}/sources`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load run: {error}</div>;
  }

  const totals = data.totals || {};

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">
          <a href={`#/${slug}/runs`} class="back-link">&larr;</a>
          {' '}Run: {date}
        </h1>
        <StatusBadge status={data.status} />
      </div>

      {/* Summary stats */}
      <div class="run-summary">
        <div class="stat">
          <div class="stat-value">
            {data.duration_s != null ? `${Math.round(data.duration_s)}s` : '-'}
          </div>
          <div class="stat-label">Duration</div>
        </div>
        {totals.cost != null && (
          <div class="stat">
            <div class="stat-value">${totals.cost.toFixed(3)}</div>
            <div class="stat-label">Cost</div>
          </div>
        )}
        {totals.input_tokens != null && (
          <div class="stat">
            <div class="stat-value">{totals.input_tokens.toLocaleString()}</div>
            <div class="stat-label">Input tokens</div>
          </div>
        )}
        {totals.output_tokens != null && (
          <div class="stat">
            <div class="stat-value">{totals.output_tokens.toLocaleString()}</div>
            <div class="stat-label">Output tokens</div>
          </div>
        )}
        {data.error && (
          <div class="stat">
            <div class="stat-value" style="color: var(--md-sys-color-error)">{data.error}</div>
            <div class="stat-label">Error</div>
          </div>
        )}
      </div>

      {/* Article funnel */}
      {funnel && (
        <div class="run-section">
          <div class="run-section-title">
            <md-icon>filter_alt</md-icon>
            Article Funnel
          </div>
          <FunnelChart funnel={funnel} />
        </div>
      )}

      {/* Stage timeline */}
      <div class="run-section">
        <div class="run-section-title">
          <md-icon>timeline</md-icon>
          Pipeline Stages
        </div>
        <StageTimeline stages={data.stages} />
      </div>

      {/* Source files */}
      {sources && sources.length > 0 && (
        <div class="run-section">
          <div class="run-section-title">
            <md-icon>source</md-icon>
            Source Files
          </div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Type</th>
                <th>Size</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {sources.map((src) => (
                <tr key={src.source_key}>
                  <td>{src.source_key}</td>
                  <td>{src.source_type}</td>
                  <td>{src.has_content ? formatBytes(src.size_bytes) : '0 B'}</td>
                  <td>
                    {src.has_content
                      ? <span class="badge badge--success">Content</span>
                      : <span class="badge badge--failure">Empty</span>
                    }
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function formatBytes(bytes) {
  if (bytes > 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes > 1_000) return `${(bytes / 1_000).toFixed(1)} KB`;
  return `${bytes} B`;
}
