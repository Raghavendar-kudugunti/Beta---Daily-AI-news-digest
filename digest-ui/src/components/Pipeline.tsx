import type { RunEvent, RunStatus } from "../types";

const NODES = [
  ["researcher", "Researcher", "pulls feeds", "⌕"], ["drafter", "Drafter", "picks top 5", "✎"], ["checker", "Checker", "verifies", "✓"],
  ["human", "Human", "approves", "☺"], ["reporter", "Reporter", "writes report", "▤"],
] as const;

function activeNode(events: RunEvent[], status: RunStatus, requireHuman: boolean): string | null {
  if (status === "awaiting_review") return "human";
  if (status !== "running") return null;
  const last = [...events].reverse().find((e) => ["researcher_done", "drafter_done", "checker_done", "human_decision", "run_done"].includes(e.event));
  if (!last) return "researcher";
  switch (last.event) {
    case "researcher_done": return "drafter";
    case "drafter_done": return "checker";
    case "checker_done": return last.passed ? (requireHuman ? "human" : "reporter") : Number(last.valid_candidates) < 5 ? "researcher" : "drafter";
    case "human_decision": return last.choice === "approve" || last.choice === "a" || last.choice === "y" ? "reporter" : "drafter";
    default: return null;
  }
}

export default function Pipeline({ events, status, requireHuman }: { events: RunEvent[]; status: RunStatus; requireHuman: boolean }) {
  const count = (name: string) => events.filter((e) => e.event === name).length;
  const done: Record<string, number> = {
    researcher: count("researcher_done"), drafter: count("drafter_done"), checker: count("checker_done"),
    human: count("human_decision"), reporter: count("run_done"),
  };
  const fails = events.filter((e) => e.event === "checker_done" && !e.passed).length;
  const active = activeNode(events, status, requireHuman);
  return (
    <div className="pipeline">
      {NODES.map(([key, label, sub, icon], i) => (
        <div key={key} className="pipe-item">
          <div className={`node ${active === key ? "active" : ""} ${done[key] > 0 ? "done" : ""}`}>
            <span className="ni">{icon}</span>
            <b>{label}</b><span className="sub2">{sub}</span>
            {done[key] > 0 && <em>{key === "checker" ? `✓${done[key] - fails} ✗${fails}` : `×${done[key]}`}</em>}
          </div>
          {i < NODES.length - 1 && <div className={`link ${active === NODES[i + 1][0] ? "flow" : ""} ${done[key] > 0 ? "lit" : ""}`} />}
        </div>
      ))}
    </div>
  );
}
