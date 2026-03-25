export function AutoRefreshToggle({ enabled, onToggle }) {
  return (
    <div class="auto-refresh">
      <span>Auto-refresh</span>
      <md-switch
        selected={enabled}
        onInput={(e) => onToggle(e.target.selected)}
      />
    </div>
  );
}
