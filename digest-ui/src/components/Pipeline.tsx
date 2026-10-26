import type { RunEvent, RunMode, RunStatus } from "../types";

type NodeInfo = readonly [key: string, label: string, sub: string, event: string];
const FOUR_ROLE_NODES: readonly NodeInfo[] = [
  ["researcher", "Researcher", "pulls feeds", "researcher_done"],
  ["drafter", "Drafter", "picks top 5", "drafter_done"],
  ["checker", "Checker", "verifies", "checker_done"],
  ["human", "Human", "approves", "human_decision"],
  ["reporter", "Reporter", "writes report", "run_done"],
];
const SINGLE_AGENT_NODES: readonly NodeInfo[] = [
  ["sources", "Sources", "feeds and search", "researcher_done"],
  ["single_agent", "Single agent", "researches + selects", "single_agent_done"],
  ["human", "Human", "approves", "human_decision"],
  ["reporter", "Reporter", "writes report", "run_done"],
];

function activeNode(events: RunEvent[], status: RunStatus, requireHuman: boolean, mode: RunMode): string | null {
  if (status === "awaiting_review") return "human";
  if (status !== "running") return null;
  const tracked = ["researcher_done", "single_agent_done", "drafter_done", "checker_done", "human_decision", "run_done"];
  const last = [...events].reverse().find((e) => tracked.includes(e.event));
  if (!last) return mode === "single_agent" ? "sources" : "researcher";
  if (mode === "single_agent") {
    switch (last.event) {
      case "researcher_done": return "single_agent";
      case "single_agent_done": return requireHuman ? "human" : "reporter";
      case "human_decision": return last.choice === "approve" || last.choice === "a" || last.choice === "y"
        ? "reporter" : "single_agent";
      default: return null;
    }
  }
  switch (last.event) {
    case "researcher_done": return "drafter";
    case "drafter_done": return "checker";
    case "checker_done": return last.passed ? (requireHuman ? "human" : "reporter") : Number(last.valid_candidates) < 5 ? "researcher" : "drafter";
    case "human_decision": return last.choice === "approve" || last.choice === "a" || last.choice === "y" ? "reporter" : "drafter";
    default: return null;
  }
}

export default function Pipeline({ events, status, requireHuman, mode }: {
  events: RunEvent[]; status: RunStatus; requireHuman: boolean; mode: RunMode;
}) {
  const nodes = mode === "single_agent" ? SINGLE_AGENT_NODES : FOUR_ROLE_NODES;
  const count = (event: string) => events.filter((e) => e.event === event).length;
  const fails = events.filter((e) => e.event === "checker_done" && !e.passed).length;
  const active = activeNode(events, status, requireHuman, mode);
  return (
    <div className="pipeline">
      {nodes.map(([key, label, sub, event], i) => {
        const completed = count(event);
        return (
          <div key={key} className="pipe-item">
            <div className={`node ${active === key ? "active" : ""} ${completed > 0 ? "done" : ""}`}>
              <span className="ni">{i + 1}</span>
              <b>{label}</b><span className="sub2">{sub}</span>
              {completed > 0 && <em>{key === "checker" ? `✓${completed - fails} ×${fails}` : `×${completed}`}</em>}
            </div>
            {i < nodes.length - 1 && <div className={`link ${active === nodes[i + 1][0] ? "flow" : ""} ${completed > 0 ? "lit" : ""}`} />}
          </div>
        );
      })}
    </div>
  );
}
