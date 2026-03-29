import { useApi } from '../hooks/useApi';
import { StatusBadge } from '../components/StatusBadge';
import { StageTimeline } from '../components/StageTimeline';

function formatSize(bytes) {
  if (bytes > 1_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  if (bytes > 1_000) return `${(bytes / 1_000).toFixed(0)} KB`;
  return `${bytes} B`;
}

export function PodcastRunDetail({ slug, date, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/podcast/runs/${date}`, refreshInterval);

  if (loading) {
    return (
      <div class="loading-container">
        <md-circular-progress indeterminate />
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <div class="page-header">
          <h1 class="page-title">
            <a href={`#/${slug}/podcast`} class="back-link">&larr;</a>
            {' '}Podcast: {date}
          </h1>
        </div>
        <div class="empty-state">No run data available for this episode.</div>
      </div>
    );
  }

  const totals = data.totals || {};

  return (
    <div>
      <div class="page-header">
        <h1 class="page-title">
          <a href={`#/${slug}/podcast`} class="back-link">&larr;</a>
          {' '}Podcast: {date}
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
        {totals.audio_duration_s != null && (
          <div class="stat">
            <div class="stat-value">{(totals.audio_duration_s / 60).toFixed(1)}m</div>
            <div class="stat-label">Audio</div>
          </div>
        )}
        {totals.mp3_size != null && (
          <div class="stat">
            <div class="stat-value">{formatSize(totals.mp3_size)}</div>
            <div class="stat-label">MP3 Size</div>
          </div>
        )}
        {data.error && (
          <div class="stat">
            <div class="stat-value" style="color: var(--md-sys-color-error)">{data.error}</div>
            <div class="stat-label">Error</div>
          </div>
        )}
      </div>

      {/* Stage timeline */}
      {data._synthetic && (
        <div class="empty-state" style="padding: 24px">
          Stage details not available for episodes that predate run logging.
        </div>
      )}
      {data.stages && data.stages.length > 0 && (
        <div class="run-section">
          <div class="run-section-title">
            <md-icon>timeline</md-icon>
            Pipeline Stages
          </div>
          <StageTimeline stages={data.stages} />
        </div>
      )}
    </div>
  );
}
