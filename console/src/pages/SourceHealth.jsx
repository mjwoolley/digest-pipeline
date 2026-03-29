import { useApi } from '../hooks/useApi';

const TYPE_ORDER = ['twitter', 'blog', 'newsletter', 'research', 'github_trending'];

function daysSince(dateStr) {
  if (!dateStr) return Infinity;
  const d = new Date(dateStr + 'T00:00:00Z');
  const now = new Date();
  return Math.floor((now - d) / (1000 * 60 * 60 * 24));
}

function healthStatus(src) {
  if (!src.last_updated) return 'stale';
  const days = daysSince(src.last_updated);
  if (days > 3) return 'stale';
  if (days > 1 && src.seen_count === 0) return 'warning';
  return 'healthy';
}

export function SourceHealth({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/sources`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load sources: {error}</div>;
  }

  if (!data || data.length === 0) {
    return <div class="empty-state">No sources configured.</div>;
  }

  // Group by type
  const grouped = {};
  for (const src of data) {
    const t = src.source_type;
    if (!grouped[t]) grouped[t] = [];
    grouped[t].push(src);
  }

  const sortedTypes = Object.keys(grouped).sort(
    (a, b) => (TYPE_ORDER.indexOf(a) === -1 ? 99 : TYPE_ORDER.indexOf(a)) -
              (TYPE_ORDER.indexOf(b) === -1 ? 99 : TYPE_ORDER.indexOf(b))
  );

  const staleCount = data.filter((s) => healthStatus(s) === 'stale').length;
  const warningCount = data.filter((s) => healthStatus(s) === 'warning').length;

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Source Health</h1>
        <div style="display: flex; gap: 12px; align-items: center">
          {staleCount > 0 && (
            <span class="badge badge--failure">{staleCount} stale</span>
          )}
          {warningCount > 0 && (
            <span class="badge badge--running">{warningCount} warning</span>
          )}
          {staleCount === 0 && warningCount === 0 && (
            <span class="badge badge--success">All healthy</span>
          )}
          <md-filled-button href={`#/${slug}/sources/new`}>
            <md-icon slot="icon">add</md-icon>
            Add Source
          </md-filled-button>
        </div>
      </div>

      {sortedTypes.map((type) => (
        <div key={type} class="run-section">
          <div class="run-section-title">
            <md-icon>
              {{ twitter: 'alternate_email', blog: 'rss_feed', newsletter: 'mail',
                 research: 'science', github_trending: 'trending_up' }[type] || 'source'}
            </md-icon>
            {type.replace('_', ' ')}
            <span style="font-weight: 400; color: var(--md-sys-color-on-surface-variant); font-size: 13px">
              ({grouped[type].length})
            </span>
          </div>
          <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>Source</th>
                <th>Last Updated</th>
                <th>Seen Items</th>
                <th>Health</th>
              </tr>
            </thead>
            <tbody>
              {grouped[type].map((src) => {
                const status = healthStatus(src);
                const days = daysSince(src.last_updated);
                return (
                  <tr
                    key={src.source_key}
                    class="clickable-row"
                    onClick={() => {
                      const [srcType, ...rest] = src.source_key.split(':');
                      const key = rest.join(':');
                      location.hash = `#/${slug}/sources/${srcType}/${key}`;
                    }}
                  >
                    <td>
                      {src.url ? (
                        <a href={src.url} target="_blank" rel="noopener" style="color: var(--md-sys-color-primary); text-decoration: none">{src.name}</a>
                      ) : src.name}
                    </td>
                    <td>
                      {src.last_updated ? (
                        <span>
                          {src.last_updated}
                          {days > 0 && <span style="color: var(--md-sys-color-on-surface-variant); margin-left: 4px">({days}d ago)</span>}
                        </span>
                      ) : (
                        <span style="color: var(--md-sys-color-error)">Never</span>
                      )}
                    </td>
                    <td>{src.seen_count}</td>
                    <td>
                      {status === 'healthy' && <span class="badge badge--success">Healthy</span>}
                      {status === 'warning' && <span class="badge badge--running">Warning</span>}
                      {status === 'stale' && <span class="badge badge--failure">Stale</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </div>
      ))}
    </div>
  );
}
