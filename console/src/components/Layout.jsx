import { useState } from 'preact/hooks';
import { AutoRefreshToggle } from './AutoRefreshToggle';

export function Layout({ slug, children, autoRefresh, onAutoRefreshChange }) {
  const nav = [
    { href: '#/', label: 'Overview', icon: 'dashboard' },
  ];

  const digestNav = slug ? [
    { href: `#/${slug}/runs`, label: 'Runs', icon: 'history' },
    { href: `#/${slug}/sources`, label: 'Sources', icon: 'rss_feed' },
    { href: `#/${slug}/delivery`, label: 'Delivery', icon: 'mail' },
    { href: `#/${slug}/podcast`, label: 'Podcast', icon: 'podcasts' },
  ] : [];

  const currentHash = typeof window !== 'undefined' ? window.location.hash : '';

  return (
    <div class="shell">
      <nav class="sidebar">
        <div class="sidebar-header">
          <md-icon>monitoring</md-icon>
          Pipeline Console
        </div>
        <md-divider />
        {nav.map(({ href, label, icon }) => (
          <a key={href} href={href} class={currentHash === href || (href === '#/' && currentHash === '') ? 'active' : ''}>
            <md-icon>{icon}</md-icon>
            {label}
          </a>
        ))}
        {digestNav.length > 0 && (
          <>
            <div class="sidebar-section">{slug}</div>
            {digestNav.map(({ href, label, icon }) => (
              <a key={href} href={href} class={currentHash.startsWith(href) ? 'active' : ''}>
                <md-icon>{icon}</md-icon>
                {label}
              </a>
            ))}
          </>
        )}
        <div style="flex: 1" />
        <md-divider />
        <div style="padding: 16px 24px">
          <AutoRefreshToggle
            enabled={autoRefresh}
            onToggle={onAutoRefreshChange}
          />
        </div>
      </nav>
      <main class="content">
        {children}
      </main>
    </div>
  );
}
