export interface Story { title: string; url: string; published: string; source: string; snippet?: string; why_it_matters: string }
export interface RunEvent { ts: string; event: string; [key: string]: unknown }
export interface Issue { pick: number | null; type: string; detail: string }
export interface RunResult {
  status: string; picks: Story[]; rejection_log: { attempt: number; issues: Issue[] }[]; doubts: string[]; report: string;
  run_mode?: "single_agent" | "four_roles";
}
export type RunStatus = "idle" | "running" | "awaiting_review" | "done" | "error";
export type RunMode = "single_agent" | "four_roles";
export interface Snapshot {
  status: RunStatus; error: string | null; pending: Story[] | null; result: RunResult | null;
  run_mode: RunMode; events: RunEvent[]; next: number;
}
export interface Config {
  main_model: string; checker_model: string; price_in: number; price_out: number;
  require_human: boolean; agent_search: boolean; llm_review: boolean;
}
export interface HistoryItem {
  name: string; started: string; status: string; mode: RunMode; calls: number; input_tokens: number; output_tokens: number;
  latency_s: number; events: number;
}
