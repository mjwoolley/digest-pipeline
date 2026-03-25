import { StatusBadge } from './StatusBadge';

export function DigestCard({ digest }) {
  const { slug, name, emoji, source_count, subscriber_count, last_run } = digest;

  return (
    <a class="card-link" href={`#/${slug}/runs`}>
      <div class="card">
        <div class="card-header">
          <span class="card-emoji">{emoji}</span>
          <span class="card-name">{name}</span>
        </div>

        {last_run ? (
          <div class="card-run">
            <div>
              <StatusBadge status={last_run.status} />
              <span class="card-run-date" style="margin-left: 8px">{last_run.date}</span>
            </div>
            <div class="card-run-meta">
              {last_run.duration_s != null && (
                <span>{Math.round(last_run.duration_s)}s</span>
              )}
              {last_run.cost != null && (
                <span>${last_run.cost.toFixed(3)}</span>
              )}
            </div>
          </div>
        ) : (
          <div class="card-run">
            <StatusBadge status={null} />
          </div>
        )}

        <div class="card-stats">
          <div class="stat">
            <div class="stat-value">{last_run?.article_count ?? '-'}</div>
            <div class="stat-label">Articles</div>
          </div>
          <div class="stat">
            <div class="stat-value">{source_count}</div>
            <div class="stat-label">Sources</div>
          </div>
          <div class="stat">
            <div class="stat-value">{subscriber_count}</div>
            <div class="stat-label">Subscribers</div>
          </div>
        </div>
      </div>
    </a>
  );
}
