import { useApi } from '../hooks/useApi';
import { StatusBadge } from '../components/StatusBadge';

function formatSize(bytes) {
  if (bytes > 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes > 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  return `${bytes} B`;
}

export function Podcasts({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/podcast`, refreshInterval);
  const { data: runs, loading: runsLoading } = useApi(`/api/digests/${slug}/podcast/runs`, refreshInterval);

  if (loading && runsLoading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load podcast data: {error}</div>;
  }

  const episodesTotal = data?.episodes?.length || 0;
  const rssInSync = data && data.rss_item_count === episodesTotal;

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Podcast</h1>
        {data && (
          <span class={`badge ${data.enabled ? 'badge--success' : 'badge--unknown'}`}>
            {data.enabled ? 'Enabled' : 'Disabled'}
          </span>
        )}
      </div>

      {/* Summary */}
      {data && (
        <div style="display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap">
          {data.name && (
            <div class="card" style="min-width: 160px">
              <div class="stat">
                <div class="stat-value" style="font-size: 16px">{data.name}</div>
                <div class="stat-label">Name</div>
              </div>
            </div>
          )}
          <div class="card" style="min-width: 100px">
            <div class="stat">
              <div class="stat-value">{episodesTotal}</div>
              <div class="stat-label">Episodes</div>
            </div>
          </div>
          <div class="card" style="min-width: 100px">
            <div class="stat">
              <div class="stat-value">{data.rss_item_count}</div>
              <div class="stat-label">RSS Items</div>
            </div>
          </div>
          <div class="card" style="min-width: 100px">
            <div class="stat">
              <span class={`badge ${rssInSync ? 'badge--success' : 'badge--failure'}`}>
                {rssInSync ? 'In Sync' : 'Out of Sync'}
              </span>
              <div class="stat-label" style="margin-top: 6px">RSS Health</div>
            </div>
          </div>
        </div>
      )}

      {/* Run history */}
      {runs && runs.length > 0 ? (
        <div class="data-table-wrapper">
        <table class="data-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Status</th>
              <th>Duration</th>
              <th>Cost</th>
              <th>Minutes</th>
              <th>Size</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.date} class="clickable-row"
                  onClick={() => { location.hash = `#/${slug}/podcast/${run.date}`; }}>
                <td>{run.date}</td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.duration_s != null ? `${Math.round(run.duration_s)}s` : '-'}</td>
                <td>{run.cost != null ? `$${run.cost.toFixed(3)}` : '-'}</td>
                <td>{run.audio_minutes != null ? `${run.audio_minutes}m` : '-'}</td>
                <td>{run.mp3_size != null ? formatSize(run.mp3_size) : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      ) : (
        <div class="empty-state">No podcast runs found.</div>
      )}
    </div>
  );
}
