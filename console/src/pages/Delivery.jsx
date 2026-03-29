import { useApi } from '../hooks/useApi';

export function Delivery({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/delivery`, refreshInterval);
  const { data: sends } = useApi(`/api/digests/${slug}/delivery/sends`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return <div class="error-message">Failed to load delivery data: {error}</div>;
  }

  // Calculate 7-day success rate
  const recent7 = (data.recent_sends || []).slice(0, 7);
  const totalSent = recent7.reduce((acc, s) => acc + s.sent, 0);
  const totalAll = recent7.reduce((acc, s) => acc + s.total, 0);
  const successRate = totalAll > 0 ? Math.round((totalSent / totalAll) * 100) : null;

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">Delivery</h1>
      </div>

      {/* Summary cards */}
      <div style="display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap">
        <div class="card" style="min-width: 120px">
          <div class="stat">
            <div class="stat-value">{data.subscriber_count}</div>
            <div class="stat-label">Subscribers</div>
          </div>
        </div>
        {successRate != null && (
          <div class="card" style="min-width: 120px">
            <div class="stat">
              <div class="stat-value" style={successRate < 100 ? 'color: var(--md-sys-color-error)' : ''}>
                {successRate}%
              </div>
              <div class="stat-label">7-Day Success</div>
            </div>
          </div>
        )}
        {recent7.length > 0 && (
          <div class="card" style="min-width: 120px">
            <div class="stat">
              <div class="stat-value">{recent7[0].date}</div>
              <div class="stat-label">Last Send</div>
            </div>
          </div>
        )}
      </div>

      {/* Send history by date */}
      {data.recent_sends && data.recent_sends.length > 0 && (
        <div class="run-section">
          <div class="run-section-title">
            <md-icon>calendar_month</md-icon>
            Send History by Date
          </div>
          <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Total</th>
                <th>Sent</th>
                <th>Failed</th>
                <th>Rate</th>
              </tr>
            </thead>
            <tbody>
              {data.recent_sends.map((s) => {
                const rate = s.total > 0 ? Math.round((s.sent / s.total) * 100) : 0;
                return (
                  <tr key={s.date}>
                    <td>{s.date}</td>
                    <td>{s.total}</td>
                    <td>{s.sent}</td>
                    <td style={s.failed > 0 ? 'color: var(--md-sys-color-error)' : ''}>{s.failed}</td>
                    <td>
                      <span class={`badge ${rate === 100 ? 'badge--success' : 'badge--failure'}`}>
                        {rate}%
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          </div>
        </div>
      )}

      {/* Recent individual sends */}
      {sends && sends.length > 0 && (
        <div class="run-section">
          <div class="run-section-title">
            <md-icon>send</md-icon>
            Recent Sends
          </div>
          <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Email</th>
                <th>Status</th>
                <th>Method</th>
              </tr>
            </thead>
            <tbody>
              {sends.slice(0, 50).map((s, i) => (
                <tr key={i}>
                  <td style="font-size: 12px">{s.timestamp?.slice(0, 19).replace('T', ' ')}</td>
                  <td>{s.email}</td>
                  <td>
                    <span class={`badge ${s.status === 'sent' ? 'badge--success' : 'badge--failure'}`}>
                      {s.status}
                    </span>
                  </td>
                  <td>{s.method}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        </div>
      )}
    </div>
  );
}
