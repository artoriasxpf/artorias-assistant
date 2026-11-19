// Dev live-reload: poll the loopback server for a web revision and reload the
// page when any served frontend file changes. It no-ops silently on non-local
// hosts or when the revision endpoint is unavailable.
(async () => {
  if (!/^(localhost|127\.0\.0\.1|\[::1\]|::1)$/.test(location.hostname)) return;
  let revision = null;
  try {
    const response = await fetch('/dev/web-revision', { cache: 'no-store' });
    if (!response.ok) return;
    revision = (await response.json()).revision;
  } catch {
    return;
  }
  setInterval(async () => {
    try {
      const response = await fetch('/dev/web-revision', { cache: 'no-store' });
      if (!response.ok) return;
      const next = (await response.json()).revision;
      if (next !== revision) location.reload();
    } catch {
      // The server may be restarting; a manual refresh will recover.
    }
  }, 1000);
})();
