import { useState, useEffect } from 'preact/hooks';
import { AutoRefreshToggle } from './AutoRefreshToggle';

function getInitialTheme() {
  const stored = localStorage.getItem('theme');
  if (stored) return stored;
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function Layout({ slug, children, autoRefresh, onAutoRefreshChange }) {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [theme, setTheme] = useState(getInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const toggleTheme = () => {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    localStorage.setItem('theme', next);
  };

  const nav = [
    { href: '#/', label: 'Overview', icon: 'dashboard' },
  ];

  const digestNav = slug ? [
    { href: `#/${slug}/sources`, label: 'Sources', icon: 'rss_feed' },
    { href: `#/${slug}/digest`, label: 'Digest', icon: 'history' },
    { href: `#/${slug}/podcast`, label: 'Podcast', icon: 'podcasts' },
    { href: `#/${slug}/delivery`, label: 'Delivery', icon: 'mail' },
  ] : [];

  const currentHash = typeof window !== 'undefined' ? window.location.hash : '';

  const closeNav = () => setSidebarOpen(false);

  return (
    <div class="shell">
      <button class="hamburger" onClick={() => setSidebarOpen(!sidebarOpen)}>
        <md-icon>{sidebarOpen ? 'close' : 'menu'}</md-icon>
      </button>
      {sidebarOpen && <div class="sidebar-overlay" onClick={closeNav} />}
      <nav class={`sidebar ${sidebarOpen ? 'sidebar--open' : ''}`}>
        <div class="sidebar-header">
          <md-icon>monitoring</md-icon>
          Pipeline Console
        </div>
        <md-divider />
        {nav.map(({ href, label, icon }) => (
          <a key={href} href={href} onClick={closeNav}
             class={currentHash === href || (href === '#/' && currentHash === '') ? 'active' : ''}>
            <md-icon>{icon}</md-icon>
            {label}
          </a>
        ))}
        {digestNav.length > 0 && (
          <>
            <div class="sidebar-section">{slug}</div>
            {digestNav.map(({ href, label, icon }) => (
              <a key={href} href={href} onClick={closeNav}
                 class={currentHash.startsWith(href) ? 'active' : ''}>
                <md-icon>{icon}</md-icon>
                {label}
              </a>
            ))}
          </>
        )}
        <div style="flex: 1" />
        <md-divider />
        <div style="padding: 16px 24px; display: flex; flex-direction: column; gap: 12px">
          <AutoRefreshToggle
            enabled={autoRefresh}
            onToggle={onAutoRefreshChange}
          />
          <button class="theme-toggle" onClick={toggleTheme}>
            <md-icon>{theme === 'dark' ? 'light_mode' : 'dark_mode'}</md-icon>
            {theme === 'dark' ? 'Light mode' : 'Dark mode'}
          </button>
        </div>
      </nav>
      <main class="content">
        {children}
      </main>
    </div>
  );
}
