import { useApi } from '../hooks/useApi';
import { DigestCard } from '../components/DigestCard';

export function Overview({ refreshInterval }) {
  const { data, loading, error } = useApi('/api/digests', refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load digests: {error}</div>;
  }

  if (!data || data.length === 0) {
    return <div class="empty-state">No digests configured.</div>;
  }

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Overview</h1>
      </div>
      <div class="card-grid">
        {data.map((digest) => (
          <DigestCard key={digest.slug} digest={digest} />
        ))}
      </div>
    </div>
  );
}
