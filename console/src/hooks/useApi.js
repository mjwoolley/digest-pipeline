import { useState, useEffect, useCallback, useRef } from 'preact/hooks';
import { fetchApi } from '../api';

export function useApi(path, refreshInterval = null) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const mountedRef = useRef(true);

  const load = useCallback(async () => {
    try {
      const result = await fetchApi(path);
      if (mountedRef.current) {
        setData(result);
        setError(null);
        setLoading(false);
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message);
        setLoading(false);
      }
    }
  }, [path]);

  useEffect(() => {
    mountedRef.current = true;
    setLoading(true);
    load();
    return () => { mountedRef.current = false; };
  }, [load]);

  useEffect(() => {
    if (!refreshInterval) return;
    const id = setInterval(load, refreshInterval);
    return () => clearInterval(id);
  }, [load, refreshInterval]);

  return { data, loading, error, refresh: load };
}
