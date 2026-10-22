import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { ApiClient, ApiError, apiUrl } from "./client";
import type { Query } from "./client";
import type { Page } from "./types";

interface ResourceState<T> { url: string | null; data?: T; error?: unknown; loading: boolean }
export function useOperation(api: ApiClient, name: string) {
  const operation = api.operation(name);
  const state = useSyncExternalStore(operation.subscribe, operation.snapshot, operation.snapshot);
  return { operation, ...state, locked: state.phase === "sending" || state.phase === "uncertain" };
}

export function useResource<T>(
  api: ApiClient, path: string | null, query: Query = {}, pollWhile?: (data: T) => boolean,
) {
  const url = path ? apiUrl(path, query) : null;
  const [state, setState] = useState<ResourceState<T>>({
    url, loading: !!url,
  });
  const [revision, setRevision] = useState(0);
  const policy = useRef(pollWhile);
  policy.current = pollWhile;
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    if (!url) {
      setState({ url, loading: false });
      return;
    }
    let alive = true;
    let inFlight = false;
    let loaded: T | undefined;
    let failures = 0;
    let unauthorized = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const load = async () => {
      if (!alive || inFlight || document.hidden || unauthorized) return;
      clearTimeout(timer);
      inFlight = true;
      setState((old) => ({ url, data: old.url === url ? old.data : undefined, loading: true }));
      try {
        loaded = await api.get<T>(url, controller.signal);
        failures = 0;
        if (alive) setState({ url, data: loaded, loading: false });
      } catch (error) {
        failures += 1;
        unauthorized = error instanceof ApiError && [401, 403].includes(error.status);
        if (alive) setState((old) => ({
          url, data: old.url === url ? old.data : undefined, error, loading: false,
        }));
      } finally {
        inFlight = false;
        if (alive && !unauthorized && !document.hidden && policy.current
          && (loaded === undefined || policy.current(loaded))) {
          timer = setTimeout(load, Math.min(3000 * 2 ** Math.min(failures, 4), 30_000));
        }
      }
    };
    const visible = () => {
      if (document.hidden) clearTimeout(timer);
      else if (loaded === undefined || (policy.current && policy.current(loaded))) void load();
    };
    document.addEventListener("visibilitychange", visible);
    void load();
    return () => {
      alive = false;
      controller.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [api, url, revision]);
  const current: ResourceState<T> = state.url === url ? state : { url, loading: !!url };
  return { ...current, refresh };
}

export function usePage<T>(
  api: ApiClient, path: string | null, filters: Query = {}, pollWhile?: (data: Page<T>) => boolean,
) {
  const scope = path ? apiUrl(path, filters) : "";
  const [position, setPosition] = useState<{ scope: string; cursors: (string | undefined)[] }>({
    scope, cursors: [undefined],
  });
  useEffect(() => {
    setPosition((previous) => previous.scope === scope ? previous : { scope, cursors: [undefined] });
  }, [scope]);
  const cursors = position.scope === scope ? position.cursors : [undefined];
  const cursor = cursors[cursors.length - 1];
  const resource = useResource<Page<T>>(api, path, { ...filters, limit: 20, cursor }, pollWhile);
  const next = () => {
    const nextCursor = resource.data?.next_cursor;
    if (nextCursor) setPosition({ scope, cursors: [...cursors, nextCursor] });
  };
  const previous = () => setPosition({ scope, cursors: cursors.length > 1 ? cursors.slice(0, -1) : cursors });
  const first = () => { setPosition({ scope, cursors: [undefined] }); resource.refresh(); };
  return { ...resource, pageNumber: cursors.length, next, previous, first };
}
