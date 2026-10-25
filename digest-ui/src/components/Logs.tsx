import { useEffect, useMemo, useRef, useState } from "react";
import type { RunEvent } from "../types";
import { describe, hhmmss, tone } from "../lib";

export default function Logs({ events, live }: { events: RunEvent[]; live: boolean }) {
  const [q, setQ] = useState("");
  const [types, setTypes] = useState<Set<string>>(new Set());
  const [problems, setProblems] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const end = useRef<HTMLDivElement>(null);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    events.forEach((e) => { c[e.event] = (c[e.event] ?? 0) + 1; });
    return c;
  }, [events]);

  const rows = events.map((e, i) => ({ e, i })).filter(({ e }) =>
    (types.size === 0 || types.has(e.event)) && (!problems || tone(e) === "bad" || tone(e) === "warn") &&
    (!q || (describe(e) + JSON.stringify(e)).toLowerCase().includes(q.toLowerCase())));

  useEffect(() => { if (live) end.current?.scrollIntoView({ block: "end" }); }, [events.length, live]);

  const toggle = (t: string) => setTypes((prev) => { const n = new Set(prev); if (n.has(t)) n.delete(t); else n.add(t); return n; });

  function download() {
    const blob = new Blob([events.map((e) => JSON.stringify(e)).join("\n")], { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "run.jsonl"; a.click(); URL.revokeObjectURL(a.href);
  }

  if (events.length === 0) return <p className="empty">No log events yet.</p>;
  return (
    <div>
      <div className="bar">
        <input className="search" placeholder="Search logs" value={q} onChange={(e) => setQ(e.target.value)} />
        <label><input type="checkbox" checked={problems} onChange={(e) => setProblems(e.target.checked)} /> Problems only</label>
        <button className="secondary" onClick={download}>Download JSONL</button>
      </div>
      <div className="chips">
        {Object.entries(counts).map(([t, n]) => (
          <button key={t} className={`chip ${types.has(t) ? "on" : ""}`} onClick={() => toggle(t)}>{t} {n}</button>
        ))}
      </div>
      <div className="logbox">
        {rows.map(({ e, i }) => (
          <div key={i} className={`logrow ${tone(e)}`} onClick={() => setOpen(open === i ? null : i)}>
            <span className="t">{hhmmss(e.ts)}</span><span className="badge">{e.event}</span><span>{describe(e)}</span>
            {open === i && <pre>{JSON.stringify(e, null, 2)}</pre>}
          </div>
        ))}
        {rows.length === 0 && <p className="empty">No events match the filters.</p>}
        <div ref={end} />
      </div>
    </div>
  );
}
