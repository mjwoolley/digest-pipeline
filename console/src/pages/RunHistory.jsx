import { useApi } from '../hooks/useApi';
import { StatusBadge } from '../components/StatusBadge';

export function RunHistory({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/runs`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load runs: {error}</div>;
  }

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Digest Runs</h1>
      </div>
      {!data || data.length === 0 ? (
        <div class="empty-state">No runs found.</div>
      ) : (
        <div class="data-table-wrapper">
        <table class="data-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Status</th>
              <th>Duration</th>
              <th>Cost</th>
              <th>Articles</th>
            </tr>
          </thead>
          <tbody>
            {data.map((run) => (
              <tr key={run.date} class="clickable-row" onClick={() => { location.hash = `#/${slug}/digest/${run.date}`; }}>
                <td>{run.date}</td>
                <td><StatusBadge status={run.status} /></td>
                <td>{run.duration_s != null ? `${Math.round(run.duration_s)}s` : '-'}</td>
                <td>{run.cost != null ? `$${run.cost.toFixed(3)}` : '-'}</td>
                <td>{run.article_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </div>
  );
}
