const BASE = '';

export async function fetchApi(path) {
  const resp = await fetch(`${BASE}${path}`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${resp.status}`);
  }
  return resp.json();
}
