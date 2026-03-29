import { useState, useEffect } from 'preact/hooks';
import { Layout } from './components/Layout';
import { Overview } from './pages/Overview';
import { RunHistory } from './pages/RunHistory';
import { RunDetail } from './pages/RunDetail';
import { SourceHealth } from './pages/SourceHealth';
import { Delivery } from './pages/Delivery';
import { Podcasts } from './pages/Podcasts';
import { PodcastRunDetail } from './pages/PodcastRunDetail';
import { SourceEdit } from './pages/SourceEdit';

const REFRESH_INTERVAL = 5000;

export function App() {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [route, setRoute] = useState(window.location.hash.slice(1) || '/');

  useEffect(() => {
    const onHashChange = () => setRoute(window.location.hash.slice(1) || '/');
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  const refreshInterval = autoRefresh ? REFRESH_INTERVAL : null;

  // Parse route
  const parts = route.split('/').filter(Boolean);
  const slug = parts.length > 0 && parts[0] !== '/' ? parts[0] : null;

  // Skip slug if it looks like a top-level route
  const isDigestRoute = slug && !['health'].includes(slug);

  let page;
  if (!isDigestRoute || parts.length === 0) {
    page = <Overview refreshInterval={refreshInterval} />;
  } else if (parts.length >= 3 && (parts[1] === 'digest' || parts[1] === 'runs')) {
    page = <RunDetail slug={slug} date={parts[2]} refreshInterval={refreshInterval} />;
  } else if (parts[1] === 'digest' || parts[1] === 'runs') {
    page = <RunHistory slug={slug} refreshInterval={refreshInterval} />;
  } else if (parts.length >= 4 && parts[1] === 'sources') {
    // Edit: /{slug}/sources/{type}/{key}
    page = <SourceEdit slug={slug} sourceType={parts[2]} sourceKey={parts[3]} refreshInterval={refreshInterval} />;
  } else if (parts.length >= 3 && parts[1] === 'sources' && parts[2] === 'new') {
    // Create: /{slug}/sources/new
    page = <SourceEdit slug={slug} refreshInterval={refreshInterval} />;
  } else if (parts[1] === 'sources') {
    page = <SourceHealth slug={slug} refreshInterval={refreshInterval} />;
  } else if (parts[1] === 'delivery') {
    page = <Delivery slug={slug} refreshInterval={refreshInterval} />;
  } else if (parts.length >= 3 && parts[1] === 'podcast') {
    page = <PodcastRunDetail slug={slug} date={parts[2]} refreshInterval={refreshInterval} />;
  } else if (parts[1] === 'podcast') {
    page = <Podcasts slug={slug} refreshInterval={refreshInterval} />;
  } else {
    page = <Overview refreshInterval={refreshInterval} />;
  }

  return (
    <Layout
      slug={isDigestRoute ? slug : null}
      autoRefresh={autoRefresh}
      onAutoRefreshChange={setAutoRefresh}
    >
      {page}
    </Layout>
  );
}
