import type { HistoryItem } from "../types";
import { fmt } from "../lib";

interface Props { items: HistoryItem[]; current: string | null; onLoad: (name: string) => void; onRefresh: () => void }

export default function History({ items, current, onLoad, onRefresh }: Props) {
  return (
    <div>
      <div className="bar"><button className="secondary" onClick={onRefresh}>Refresh</button>
        <small className="muted">Past runs are read from the logs folder.</small></div>
      {items.length === 0 && <p className="empty">No saved runs yet.</p>}
      <table>
        <thead><tr><th>Started</th><th>Status</th><th>Calls</th><th>Tokens</th><th>Model time</th><th /></tr></thead>
        <tbody>{items.map((r) => (
          <tr key={r.name} className={current === r.name ? "sel" : ""}>
            <td>{r.started.replace("T", " ").slice(0, 19)}</td><td>{r.status}</td><td>{r.calls}</td>
            <td>{fmt(r.input_tokens + r.output_tokens)}</td><td>{r.latency_s}s</td>
            <td><button className="secondary" onClick={() => onLoad(r.name)}>View</button></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}
