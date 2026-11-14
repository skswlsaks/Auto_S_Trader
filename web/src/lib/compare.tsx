"use client";
/* Compare list shared across pages: up to 4 runs, each keeps its colour slot until removed. */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export const SLOTS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)"];
type Entry = { id: string; slot: number };
interface Ctx { list: Entry[]; has: (id: string) => boolean; slot: (id: string) => number | null; color: (id: string) => string | null;
  toggle: (id: string) => boolean; set: (ids: string[]) => void; clear: () => void }
const C = createContext<Ctx | null>(null);
const KEY = "arena-compare";

export function CompareProvider({ children }: { children: ReactNode }) {
  const [list, setList] = useState<Entry[]>([]);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    try { setList(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch { /* ignore a corrupt entry */ }
    setLoaded(true);
  }, []);
  // write only after the saved list was read, so the initial empty state never overwrites it
  useEffect(() => { if (loaded) localStorage.setItem(KEY, JSON.stringify(list)); }, [list, loaded]);
  const toggle = useCallback((id: string) => {
    let ok = true;
    setList(l => {
      if (l.some(e => e.id === id)) return l.filter(e => e.id !== id);
      const used = new Set(l.map(e => e.slot)), free = [0, 1, 2, 3].find(s => !used.has(s));
      if (free == null) { ok = false; return l; }
      return [...l, { id, slot: free }];
    });
    return ok;
  }, []);
  const value = useMemo<Ctx>(() => ({
    list, has: id => list.some(e => e.id === id), slot: id => list.find(e => e.id === id)?.slot ?? null,
    color: id => { const s = list.find(e => e.id === id)?.slot; return s == null ? null : SLOTS[s]; },
    toggle, set: ids => setList(ids.slice(0, 4).map((id, slot) => ({ id, slot }))), clear: () => setList([]),
  }), [list, toggle]);
  return <C.Provider value={value}>{children}</C.Provider>;
}
export function useCompare() { const v = useContext(C); if (!v) throw new Error("CompareProvider missing"); return v; }
