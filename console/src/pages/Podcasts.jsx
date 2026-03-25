import { useApi } from '../hooks/useApi';

function formatSize(bytes) {
  if (bytes > 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes > 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  return `${bytes} B`;
}

export function Podcasts({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/podcast`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load podcast data: {error}</div>;
  }

  const episodesWithScript = data.episodes?.filter((e) => e.has_script).length || 0;
  const episodesTotal = data.episodes?.length || 0;
  const rssInSync = data.rss_item_count === episodesTotal;

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Podcast</h1>
        <span class={`badge ${data.enabled ? 'badge--success' : 'badge--unknown'}`}>
          {data.enabled ? 'Enabled' : 'Disabled'}
        </span>
      </div>

      {/* Summary */}
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

      {/* Episode list */}
      {episodesTotal > 0 ? (
        <table class="data-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>MP3</th>
              <th>Size</th>
              <th>Script</th>
            </tr>
          </thead>
          <tbody>
            {data.episodes.map((ep) => (
              <tr key={ep.date}>
                <td>{ep.date}</td>
                <td>
                  {ep.has_mp3
                    ? <span class="badge badge--success">Yes</span>
                    : <span class="badge badge--failure">Missing</span>}
                </td>
                <td>{ep.has_mp3 ? formatSize(ep.mp3_size) : '-'}</td>
                <td>
                  {ep.has_script
                    ? <span class="badge badge--success">Yes</span>
                    : <span class="badge badge--failure">Missing</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <div class="empty-state">No episodes found.</div>
      )}
    </div>
  );
}
