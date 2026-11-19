import "@testing-library/jest-dom/vitest";

/*
 * Node 22+ defines a `localStorage` global of its own, which is undefined unless
 * Node starts with --localstorage-file, and it hides jsdom's. The components that
 * remember a layout preference need a working Storage, so the tests get an
 * in-memory one.
 */
if (typeof window !== "undefined" && !window.localStorage) {
  const items = new Map<string, string>();
  const memory: Storage = {
    get length() {
      return items.size;
    },
    clear: () => items.clear(),
    getItem: (key) => items.get(key) ?? null,
    key: (index) => [...items.keys()][index] ?? null,
    removeItem: (key) => void items.delete(key),
    setItem: (key, value) => void items.set(key, String(value)),
  };
  Object.defineProperty(window, "localStorage", { value: memory, configurable: true });
}
