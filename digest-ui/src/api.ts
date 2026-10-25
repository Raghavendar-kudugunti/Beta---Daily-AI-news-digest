import type { Config, HistoryItem, RunEvent, Snapshot } from "./types";

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({ detail: res.statusText }))).detail);
  return res.json() as Promise<T>;
}

export const getState = (since: number) => call<Snapshot>(`/api/state?since=${since}`);
export const startRun = (requireHuman: boolean) => call<{ ok: boolean }>("/api/runs", { require_human: requireHuman });
export const decide = (approve: boolean, feedback: string) => call<{ ok: boolean }>("/api/decision", { approve, feedback });
export const getConfig = () => call<Config>("/api/config");
export const getHistory = () => call<HistoryItem[]>("/api/history");
export const getHistoryRun = (name: string) => call<RunEvent[]>(`/api/history/${name}`);
