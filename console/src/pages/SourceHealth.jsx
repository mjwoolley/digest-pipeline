import { useState } from 'preact/hooks';
import { useApi } from '../hooks/useApi';

const TYPE_ORDER = ['twitter', 'blog', 'newsletter', 'research', 'github_trending'];
const TYPE_ICONS = {
  twitter: 'alternate_email', blog: 'rss_feed', newsletter: 'mail',
  research: 'science', github_trending: 'trending_up',
};

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

function yieldClass(rate) {
  if (rate == null) return '';
  if (rate >= 0.3) return 'metric-good';
  if (rate >= 0.1) return 'metric-ok';
  return 'metric-low';
}

function scoreClass(score) {
  if (score == null) return '';
  if (score >= 7) return 'metric-good';
  if (score >= 4) return 'metric-ok';
  return 'metric-low';
}

function fmtPct(rate) {
  if (rate == null) return '\u2014';
  return Math.round(rate * 100) + '%';
}

function fmtScore(score) {
  if (score == null) return '\u2014';
  return score.toFixed(1);
}

const COLUMNS = [
  { key: 'name', label: 'Source', align: 'left' },
  { key: 'extracted', label: 'Extracted', align: 'right', numeric: true },
  { key: 'in_digest', label: 'In Digest', align: 'right', numeric: true },
  { key: 'yield_rate', label: 'Yield', align: 'right', numeric: true },
  { key: 'avg_score', label: 'Avg Score', align: 'right', numeric: true },
  { key: 'last_updated', label: 'Last Updated', align: 'left' },
  { key: 'health', label: 'Health', align: 'left' },
];

function sortVal(src, key) {
  if (key === 'health') {
    const s = healthStatus(src);
    return s === 'healthy' ? 0 : s === 'warning' ? 1 : 2;
  }
  const v = src[key];
  if (v == null) return -Infinity;
  return v;
}

function compareSources(a, b, sortKey, sortDir) {
  let av = sortVal(a, sortKey);
  let bv = sortVal(b, sortKey);
  if (typeof av === 'string') av = av.toLowerCase();
  if (typeof bv === 'string') bv = bv.toLowerCase();
  if (av < bv) return sortDir === 'asc' ? -1 : 1;
  if (av > bv) return sortDir === 'asc' ? 1 : -1;
  return 0;
}

function SourceRow({ src, slug }) {
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
        <span style="display: inline-flex; align-items: center; gap: 6px">
          <md-icon style="font-size: 16px; opacity: 0.6">{TYPE_ICONS[src.source_type] || 'source'}</md-icon>
          {src.url ? (
            <a href={src.url} target="_blank" rel="noopener" style="color: var(--md-sys-color-primary); text-decoration: none">{src.name}</a>
          ) : src.name}
        </span>
      </td>
      <td style="text-align: right">{src.extracted || 0}</td>
      <td style="text-align: right">{src.in_digest || 0}</td>
      <td style="text-align: right">
        <span class={yieldClass(src.yield_rate)}>{fmtPct(src.yield_rate)}</span>
      </td>
      <td style="text-align: right">
        <span class={scoreClass(src.avg_score)}>{fmtScore(src.avg_score)}</span>
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
      <td>
        {status === 'healthy' && <span class="badge badge--success">Healthy</span>}
        {status === 'warning' && <span class="badge badge--running">Warning</span>}
        {status === 'stale' && <span class="badge badge--failure">Stale</span>}
      </td>
    </tr>
  );
}

function SortHeader({ col, sortKey, sortDir, onSort }) {
  const active = sortKey === col.key;
  const arrow = active ? (sortDir === 'asc' ? ' \u25B2' : ' \u25BC') : '';
  return (
    <th
      style={`text-align: ${col.align}; cursor: pointer; user-select: none; white-space: nowrap`}
      onClick={() => onSort(col.key)}
    >
      {col.label}{arrow}
    </th>
  );
}

export function SourceHealth({ slug, refreshInterval }) {
  const { data, loading, error } = useApi(`/api/digests/${slug}/sources`, refreshInterval);
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState('desc');

  function handleSort(key) {
    if (sortKey === key) {
      if (sortDir === 'desc') setSortDir('asc');
      else { setSortKey(null); setSortDir('desc'); } // third click resets
    } else {
      setSortKey(key);
      // Default to desc for numeric columns, asc for text
      const col = COLUMNS.find(c => c.key === key);
      setSortDir(col && col.numeric ? 'desc' : 'asc');
    }
  }

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

  const staleCount = data.filter((s) => healthStatus(s) === 'stale').length;
  const warningCount = data.filter((s) => healthStatus(s) === 'warning').length;
  const isSorted = sortKey !== null;

  // When sorted: flat list. When unsorted: grouped by type.
  let sections;
  if (isSorted) {
    const sorted = [...data].sort((a, b) => compareSources(a, b, sortKey, sortDir));
    sections = [{ type: null, sources: sorted }];
  } else {
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
    sections = sortedTypes.map(t => ({ type: t, sources: grouped[t] }));
  }

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

      {sections.map(({ type, sources }) => (
        <div key={type || 'all'} class="run-section">
          {type && (
            <div class="run-section-title">
              <md-icon>{TYPE_ICONS[type] || 'source'}</md-icon>
              {type.replace('_', ' ')}
              <span style="font-weight: 400; color: var(--md-sys-color-on-surface-variant); font-size: 13px">
                ({sources.length})
              </span>
            </div>
          )}
          <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                {COLUMNS.map(col => (
                  <SortHeader key={col.key} col={col} sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                ))}
              </tr>
            </thead>
            <tbody>
              {sources.map(src => <SourceRow key={src.source_key} src={src} slug={slug} />)}
            </tbody>
          </table>
          </div>
        </div>
      ))}
    </div>
  );
}
