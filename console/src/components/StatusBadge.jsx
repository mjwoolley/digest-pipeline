export function StatusBadge({ status }) {
  const cls = {
    success: 'badge--success',
    failure: 'badge--failure',
    running: 'badge--running',
  }[status] || 'badge--unknown';

  const label = {
    success: 'Success',
    failure: 'Failed',
    running: 'Running',
  }[status] || 'No data';

  return <span class={`badge ${cls}`}>{label}</span>;
}
