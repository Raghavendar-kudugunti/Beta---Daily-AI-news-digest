import type { RunEvent } from "./types";

export const num = (v: unknown): number => (typeof v === "number" ? v : 0);
export const fmt = (n: number) => Math.round(n).toLocaleString();
export const hhmmss = (ts: string) => ts.slice(11, 19);
export const stepColor = (s: string) =>
  ({ researcher: "var(--c1)", drafter: "var(--c2)", checker_llm: "var(--c3)" } as Record<string, string>)[s] ?? "var(--c4)";

export function describe(e: RunEvent): string {
  switch (e.event) {
    case "llm_call": return `${e.step} model call: ${e.input_tokens} in / ${e.output_tokens} out tokens, ${e.latency_s}s`;
    case "feed_ok": return `Feed ${e.feed}: ${e.items} recent items`;
    case "feed_error": return `Feed failed: ${e.feed} (${e.error})`;
    case "tool_call": return `Tool ${e.tool}: ${e.results} results`;
    case "tool_error": return `Tool error in ${e.tool}: ${e.error}`;
    case "researcher_done": return `Researcher done: ${e.candidates} candidates, ${e.eligible} eligible`;
    case "single_agent_done": return `Single agent chose ${e.picks} of ${e.pool} eligible stories`;
    case "run_started": return `Started ${e.mode === "single_agent" ? "single-agent" : "four-role"} run`;
    case "drafter_done": return `Drafter chose ${e.picks} of ${e.pool} eligible stories`;
    case "checker_done": return e.passed ? "Checker: PASS" : `Checker: REJECT (${e.issues} issues, ${e.valid_candidates} valid candidates left)`;
    case "rate_limited": return `Rate limited in ${e.step}; waiting ${e.wait_s}s`;
    case "parse_fail": return `Could not parse model output in ${e.step}`;
    case "human_decision": return `Human decision: ${e.choice}`;
    case "run_done": return `Run finished: ${e.status}`;
    default: return e.event;
  }
}

export type Tone = "good" | "bad" | "warn" | "info";
export function tone(e: RunEvent): Tone {
  if (["feed_error", "tool_error", "parse_fail"].includes(e.event)) return "bad";
  if (e.event === "checker_done") return e.passed ? "good" : "bad";
  if (e.event === "rate_limited") return "warn";
  if (e.event === "run_done") return "good";
  return "info";
}

export interface StepStat { calls: number; input: number; output: number; latency: number }
export interface Metrics {
  calls: number; input: number; output: number; latencies: { step: string; v: number }[]; avg: number; p95: number;
  total: number; perStep: Record<string, StepStat>; rateLimited: number; waited: number; feedOk: number;
  feedErrors: string[]; toolCalls: number; parseFails: number; passes: number; rejections: number;
  candidates: number; eligible: number;
}

export function computeMetrics(events: RunEvent[]): Metrics {
  const m: Metrics = { calls: 0, input: 0, output: 0, latencies: [], avg: 0, p95: 0, total: 0, perStep: {}, rateLimited: 0,
    waited: 0, feedOk: 0, feedErrors: [], toolCalls: 0, parseFails: 0, passes: 0, rejections: 0, candidates: 0, eligible: 0 };
  for (const e of events) {
    if (e.event === "llm_call") {
      const step = String(e.step);
      const s = m.perStep[step] ?? { calls: 0, input: 0, output: 0, latency: 0 };
      s.calls++; s.input += num(e.input_tokens); s.output += num(e.output_tokens); s.latency += num(e.latency_s);
      m.perStep[step] = s;
      m.calls++; m.input += num(e.input_tokens); m.output += num(e.output_tokens);
      m.latencies.push({ step, v: num(e.latency_s) }); m.total += num(e.latency_s);
    } else if (e.event === "rate_limited") { m.rateLimited++; m.waited += num(e.wait_s); }
    else if (e.event === "feed_ok") m.feedOk++;
    else if (e.event === "feed_error") m.feedErrors.push(`${e.feed}: ${e.error}`);
    else if (e.event === "tool_call") m.toolCalls++;
    else if (e.event === "parse_fail") m.parseFails++;
    else if (e.event === "checker_done") { if (e.passed) m.passes++; else m.rejections++; }
    else if (e.event === "researcher_done") { m.candidates = num(e.candidates); m.eligible = num(e.eligible); }
  }
  const sorted = m.latencies.map((l) => l.v).sort((a, b) => a - b);
  m.avg = sorted.length ? m.total / sorted.length : 0;
  m.p95 = sorted.length ? sorted[Math.max(0, Math.ceil(0.95 * sorted.length) - 1)] : 0;
  return m;
}

export const cost = (m: Metrics, pin: number, pout: number) => (m.input / 1e6) * pin + (m.output / 1e6) * pout;
