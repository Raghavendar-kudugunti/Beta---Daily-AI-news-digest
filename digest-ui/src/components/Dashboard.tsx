import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { Config, RunEvent } from "../types";
import { computeMetrics, cost, fmt, stepColor } from "../lib";

interface Props {
  events: RunEvent[]; config: Config | null; prices: { in: number; out: number };
  setPrices: (p: { in: number; out: number }) => void; title: string;
}

function useCountUp(target: number) {
  const [v, setV] = useState(target);
  const prev = useRef(0);
  useEffect(() => {
    const from = prev.current; const t0 = performance.now(); let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / 700);
      const val = from + (target - from) * (1 - Math.pow(1 - p, 3));
      setV(val); prev.current = val;
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target]);
  return v;
}

function Kpi({ label, icon, color, value, format, hint }: {
  label: string; icon: string; color: string; value: number | string; format?: (n: number) => string; hint?: string;
}) {
  const n = useCountUp(typeof value === "number" ? value : 0);
  return (
    <div className="kpi" style={{ "--k": color } as CSSProperties}>
      <i className="ic">{icon}</i><span>{label}</span>
      <b>{typeof value === "number" ? (format ?? fmt)(n) : value}</b>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function Donut({ parts }: { parts: { label: string; value: number; color: string }[] }) {
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  const R = 52, C = 2 * Math.PI * R;
  let acc = 0;
  return (
    <div className="donut-wrap">
      <svg viewBox="0 0 140 140" className="donut">
        <circle cx="70" cy="70" r={R} fill="none" stroke="var(--line)" strokeWidth="16" />
        {parts.map((p) => {
          const len = (p.value / total) * C; const off = -acc; acc += len;
          return <circle key={p.label} cx="70" cy="70" r={R} fill="none" stroke={p.color} strokeWidth="16"
            strokeDasharray={`${len} ${C - len}`} strokeDashoffset={off} transform="rotate(-90 70 70)" className="seg" />;
        })}
        <text x="70" y="68" textAnchor="middle" className="dn">{fmt(total === 1 && !parts.length ? 0 : total)}</text>
        <text x="70" y="84" textAnchor="middle" className="dl">tokens</text>
      </svg>
      <ul className="dleg">{parts.map((p) => (
        <li key={p.label}><i style={{ background: p.color }} />{p.label}<b>{Math.round((p.value / total) * 100)}%</b></li>))}</ul>
    </div>
  );
}

export default function Dashboard({ events, config, prices, setPrices, title }: Props) {
  const m = useMemo(() => computeMetrics(events), [events]);
  const total = cost(m, prices.in, prices.out);
  const maxLat = Math.max(1, ...m.latencies.map((l) => l.v));
  const steps = Object.entries(m.perStep);
  const maxTok = Math.max(1, ...steps.map(([, s]) => s.input + s.output));
  if (events.length === 0) return <p className="empty">No run data yet. Start a run, or load one from History.</p>;
  return (
    <div className="dash">
      <p className="sub">{title}</p>
      <div className="kpis">
        <Kpi label="Model calls" icon="⚡" color="var(--c1)" value={m.calls} hint={`${m.toolCalls} tool calls`} />
        <Kpi label="Tokens" icon="◈" color="var(--c2)" value={m.input + m.output} hint={`${fmt(m.input)} in / ${fmt(m.output)} out`} />
        <Kpi label="Estimated cost" icon="$" color="var(--good)" value={total} format={(n) => `$${n.toFixed(4)}`} hint="at the prices below" />
        <Kpi label="Avg latency" icon="◷" color="var(--c3)" value={m.avg} format={(n) => `${n.toFixed(1)}s`} hint={`p95 ${m.p95.toFixed(1)}s`} />
        <Kpi label="Model time" icon="⏱" color="var(--c4)" value={m.total} format={(n) => `${n.toFixed(1)}s`} hint="sum of calls" />
        <Kpi label="Checker" icon="✓" color="var(--good)" value={`${m.passes} pass / ${m.rejections} reject`} />
        <Kpi label="Candidates" icon="☰" color="var(--c1)" value={m.candidates} hint={`${fmt(m.eligible)} eligible`} />
        <Kpi label="Rate limits" icon="⚠" color="var(--warn)" value={m.rateLimited} hint={`${m.waited}s waited`} />
      </div>

      <div className="two">
        <section className="panel">
          <h3>Token share by step</h3>
          <Donut parts={steps.map(([s, v]) => ({ label: s, value: v.input + v.output, color: stepColor(s) }))} />
        </section>
        <section className="panel">
          <h3>Input vs output tokens</h3>
          {steps.map(([s, v]) => (
            <div key={s} className="hbar">
              <span className="lbl">{s}</span>
              <div className="track">
                <div style={{ width: `${(v.input / maxTok) * 100}%`, background: stepColor(s) }} title={`${v.input} input`} />
                <div style={{ width: `${(v.output / maxTok) * 100}%`, background: stepColor(s), opacity: 0.45 }} title={`${v.output} output`} />
              </div>
              <span className="val">{fmt(v.input + v.output)}</span>
            </div>
          ))}
          <small className="muted">Solid = input, faded = output</small>
        </section>
      </div>

      <section className="panel">
        <h3>Latency per model call</h3>
        <div className="spark">
          {m.latencies.map((l, i) => (
            <div key={i} className="col" data-tip={`#${i + 1} ${l.step}: ${l.v}s`}>
              <div style={{ height: `${(l.v / maxLat) * 100}%`, background: stepColor(l.step) }} />
            </div>
          ))}
        </div>
        <div className="legend">{steps.map(([s]) => <span key={s}><i style={{ background: stepColor(s) }} />{s}</span>)}</div>
      </section>

      <section className="panel">
        <h3>Per-step breakdown</h3>
        <table>
          <thead><tr><th>Step</th><th>Calls</th><th>Input</th><th>Output</th><th>Avg latency</th><th>Cost</th></tr></thead>
          <tbody>{steps.map(([s, v]) => (
            <tr key={s}><td><i className="sdot" style={{ background: stepColor(s) }} />{s}</td><td>{v.calls}</td><td>{fmt(v.input)}</td><td>{fmt(v.output)}</td>
              <td>{(v.latency / v.calls).toFixed(1)}s</td>
              <td>${((v.input / 1e6) * prices.in + (v.output / 1e6) * prices.out).toFixed(4)}</td></tr>
          ))}</tbody>
        </table>
        <div className="prices">
          <label>Price per 1M input tokens $<input type="number" min="0" step="0.01" value={prices.in}
            onChange={(e) => setPrices({ ...prices, in: Number(e.target.value) })} /></label>
          <label>per 1M output tokens $<input type="number" min="0" step="0.01" value={prices.out}
            onChange={(e) => setPrices({ ...prices, out: Number(e.target.value) })} /></label>
        </div>
        <small className="muted">Free tiers cost $0. Enter a paid model's prices to see what this run would cost.</small>
      </section>

      <section className="panel">
        <h3>Run health</h3>
        <ul className="health">
          <li>{m.feedOk} feeds read OK, {m.feedErrors.length} failed</li>
          {m.feedErrors.map((f) => <li key={f} className="bad">{f}</li>)}
          <li className={m.parseFails ? "bad" : ""}>{m.parseFails} unparseable model replies</li>
          <li className={m.rateLimited ? "warn" : ""}>{m.rateLimited} rate-limit waits ({m.waited}s)</li>
          {config && <li>Models: {config.main_model} (research and draft), {config.checker_model} (checker)</li>}
        </ul>
      </section>
    </div>
  );
}
