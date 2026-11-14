"use client";
import { useEffect, useState } from "react";
import type { ArenaIndex, CellDetail, LabSource, RunDetail, Study } from "./types";

const cache = new Map<string, Promise<unknown>>();

export class SessionExpired extends Error {}

export function load<T>(path: string, fresh = false): Promise<T> {
  if (!fresh && cache.has(path)) return cache.get(path) as Promise<T>;
  const p = fetch(path, { cache: "no-store" }).then(async r => {
    if (r.status === 401) {
      // Cognito session expired: a full reload goes through the login redirect
      if (typeof window !== "undefined") window.location.reload();
      throw new SessionExpired("세션이 만료되었습니다");
    }
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json() as Promise<T>;
  });
  cache.set(path, p);
  p.catch(() => cache.delete(path));
  return p;
}

export interface Loaded<T> { data: T | null; error: Error | null; loading: boolean }

export function useData<T>(path: string | null): Loaded<T> {
  const [state, setState] = useState<Loaded<T>>({ data: null, error: null, loading: !!path });
  useEffect(() => {
    if (!path) return;
    let live = true;
    setState(s => ({ ...s, loading: true }));
    load<T>(path).then(
      data => live && setState({ data, error: null, loading: false }),
      error => live && setState({ data: null, error, loading: false }),
    );
    return () => { live = false; };
  }, [path]);
  return state;
}

export const useIndex = () => useData<ArenaIndex>("/data/index.json");
export const useRun = (id: string | null) => useData<RunDetail>(id ? `/data/runs/${encodeURIComponent(id)}.json` : null);
export const useStudy = (id: string | null) => useData<Study>(id ? `/data/studies/${encodeURIComponent(id)}/study.json` : null);
export const useCell = (study: string | null, cell: string | null) =>
  useData<CellDetail>(study && cell ? `/data/studies/${encodeURIComponent(study)}/cells/${cell}.json` : null);
export const useLabSource = (file: string | null) => useData<LabSource>(file ? `/data/lab/${file}` : null);

/** Poll index.json and report when a newer build is available. */
export function useNewerBuild(current: string | undefined, everyMs = 60_000) {
  const [newer, setNewer] = useState<string | null>(null);
  useEffect(() => {
    if (!current) return;
    const t = setInterval(() => {
      load<ArenaIndex>("/data/index.json", true).then(d => { if (d.generated_at !== current) setNewer(d.generated_at); }, () => {});
    }, everyMs);
    return () => clearInterval(t);
  }, [current, everyMs]);
  return newer;
}
