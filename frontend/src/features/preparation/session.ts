import { useSyncExternalStore } from "react";

interface Entry<T> { value: T; dirty: boolean }

/** Only page-session state. PostgreSQL remains authoritative after a successful write. */
export class PreparationSession {
  private entries = new Map<string, Entry<unknown>>();
  private listeners = new Set<() => void>();
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  get<T>(key: string, initial: () => T): Entry<T> {
    if (!this.entries.has(key)) this.entries.set(key, { value: initial(), dirty: false });
    return this.entries.get(key) as Entry<T>;
  }
  set<T>(key: string, value: T, dirty = true) {
    this.entries.set(key, { value, dirty });
    this.listeners.forEach((listener) => listener());
  }
  hasDirty = () => [...this.entries.values()].some((entry) => entry.dirty);
  clear() { this.entries.clear(); this.listeners.forEach((listener) => listener()); }
}

export function usePreparationState<T>(store: PreparationSession, key: string, initial: () => T) {
  const entry = useSyncExternalStore(store.subscribe, () => store.get(key, initial));
  return {
    ...entry,
    set: (value: T, dirty = true) => store.set(key, value, dirty),
    update: (change: (value: T) => T, dirty = true) => store.set(key, change(store.get(key, initial).value), dirty),
  };
}
