import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { decide, getConfig, getHistory, getHistoryRun, getState, startRun } from "./api";
import type { Config, HistoryItem, RunEvent, RunMode, RunResult, RunStatus, Story } from "./types";
import Pipeline from "./components/Pipeline";
import Dashboard from "./components/Dashboard";
import Logs from "./components/Logs";
import History from "./components/History";
import { describe } from "./lib";

type Tab = "run" | "dashboard" | "logs" | "history";
const TABS: [Tab, string, string][] = [["run", "Run", "▶"], ["dashboard", "Dashboard", "◔"], ["logs", "Logs", "≡"], ["history", "History", "⟲"]];
const QUICK = ["Prefer research and model releases", "Fewer product announcements", "Avoid duplicate coverage", "Prefer primary sources"];

function StoryCard({ story, n }: { story: Story; n: number }) {
  return (
    <li className="card story" style={{ "--i": n } as CSSProperties}>
      <span className="num">{n}</span>
      <div>
        <a href={story.url} target="_blank" rel="noreferrer">{story.title}</a>
        <div className="meta">{story.source} · {story.published.replace("T", " ").slice(0, 16)} UTC</div>
        <p>{story.why_it_matters}</p>
        {story.snippet && <details><summary>Source text</summary><p className="muted">{story.snippet}</p></details>}
      </div>
    </li>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("run");
  const [status, setStatus] = useState<RunStatus>("idle");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [pending, setPending] = useState<Story[] | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [requireHuman, setRequireHuman] = useState(true);
  const [runMode, setRunMode] = useState<RunMode>("four_roles");
  const [feedback, setFeedback] = useState("");
  const [config, setConfig] = useState<Config | null>(null);
  const [prices, setPrices] = useState({ in: 0, out: 0 });
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [viewing, setViewing] = useState<{ name: string; events: RunEvent[] } | null>(null);
  const [copied, setCopied] = useState(false);
  const [theme, setTheme] = useState<"dark" | "light">(() => {
    try { const t = localStorage.getItem("theme"); if (t === "light" || t === "dark") return t; } catch { /* ignore */ }
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  });
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch { /* ignore */ }
  }, [theme]);
  const next = useRef(0);
  const active = status === "running" || status === "awaiting_review";
  const shown = viewing ? viewing.events : events;

  const refreshHistory = useCallback(() => { getHistory().then(setHistory).catch(() => undefined); }, []);

  useEffect(() => {
    getConfig().then((c) => { setConfig(c); setPrices({ in: c.price_in, out: c.price_out }); }).catch((e) => setError((e as Error).message));
    refreshHistory();
  }, [refreshHistory]);

  useEffect(() => {
    if (!active) return;
    const id = setInterval(async () => {
      try {
        const s = await getState(next.current);
        next.current = s.next;
        setEvents((prev) => [...prev, ...s.events]);
        setRunMode(s.run_mode);
        setStatus(s.status); setPending(s.pending); setResult(s.result); setError(s.error);
        if (s.status === "done" || s.status === "error") refreshHistory();
      } catch (e) { setError((e as Error).message); }
    }, 1200);
    return () => clearInterval(id);
  }, [active, refreshHistory]);

  async function start() {
    setError(null); setResult(null); setEvents([]); setViewing(null); next.current = 0;
    try { await startRun(requireHuman, runMode); setStatus("running"); } catch (e) { setError((e as Error).message); }
  }

  async function review(approve: boolean) {
    try { await decide(approve, feedback); setFeedback(""); setPending(null); setStatus("running"); }
    catch (e) { setError((e as Error).message); }
  }

  async function loadRun(name: string) {
    try {
      const pastEvents = await getHistoryRun(name);
      const mode = pastEvents.find((e) => e.event === "run_started")?.mode;
      setRunMode(mode === "single_agent" ? "single_agent" : "four_roles");
      setViewing({ name, events: pastEvents }); setTab("dashboard");
    }
    catch (e) { setError((e as Error).message); }
  }

  function copyDigest() {
    if (!result) return;
    const md = result.picks.map((p, i) => `${i + 1}. [${p.title}](${p.url})\n   ${p.why_it_matters}`).join("\n");
    navigator.clipboard.writeText(md).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); });
  }

  const latest = events.length ? describe(events[events.length - 1]) : "Ready";

  return (
    <main>
      <header>
        <div className="brand">
          <div className="logo">✦</div>
          <div><h1>Daily AI News Digest</h1>
            <p className="sub">{config ? `${config.main_model} · checker ${config.checker_model}` : "connecting to the server…"}</p></div>
        </div>
        <div className="head-right">
          <button className="icon" title="Toggle theme" onClick={() => setTheme(theme === "dark" ? "light" : "dark")}>{theme === "dark" ? "☀" : "☾"}</button>
          <span className={`dot ${status}`}><i />{status.replace("_", " ")}</span>
        </div>
      </header>
      <nav className="tabs">{TABS.map(([k, label, icon]) => (
        <button key={k} className={tab === k ? "tab on" : "tab"} onClick={() => setTab(k)}><span>{icon}</span>{label}</button>))}</nav>

      {viewing && (
        <div className="banner">Viewing past run {viewing.name}
          <button className="secondary" onClick={() => setViewing(null)}>Back to live</button></div>
      )}
      {error && <div className="error">{error}</div>}

      {tab === "run" && (
        <>
          <div className="mode-picker" role="group" aria-label="Choose digest workflow">
            <button className={`mode-option ${runMode === "single_agent" ? "selected" : ""}`} disabled={active}
              onClick={() => setRunMode("single_agent")}>
              <b>Day 1 · Single agent</b><small>One agent researches and selects the digest.</small>
            </button>
            <button className={`mode-option ${runMode === "four_roles" ? "selected" : ""}`} disabled={active}
              onClick={() => setRunMode("four_roles")}>
              <b>Day 2 · Four roles</b><small>Researcher, Drafter, Checker, and Reporter.</small>
            </button>
          </div>
          <div className="bar">
            <button onClick={start} disabled={active}>{active ? "Running…" : runMode === "single_agent" ? "Run single-agent digest" : "Run four-role digest"}</button>
            <label><input type="checkbox" checked={requireHuman} disabled={active}
              onChange={(e) => setRequireHuman(e.target.checked)} /> Require my approval</label>
            <small className="muted">{active ? latest : ""}</small>
          </div>
          <Pipeline events={events} status={status} requireHuman={requireHuman} mode={runMode} />
          {active && !pending && !result && <ol><li className="card skeleton" /><li className="card skeleton" /><li className="card skeleton" /></ol>}

          {pending && (
            <section className="review">
              <h2>Review the top {pending.length}</h2>
              <ol>{pending.map((s, i) => <StoryCard key={s.url} story={s} n={i + 1} />)}</ol>
              <div className="chips">{QUICK.map((q) => (
                <button key={q} className="chip" onClick={() => setFeedback((f) => (f ? `${f}. ${q}` : q))}>+ {q}</button>))}</div>
              <textarea placeholder="If rejecting, tell the drafter what to change" value={feedback}
                onChange={(e) => setFeedback(e.target.value)} />
              <div className="bar">
                <button onClick={() => review(true)}>Approve</button>
                <button className="secondary" onClick={() => review(false)}>Reject and redraft</button>
              </div>
            </section>
          )}

          {result && (
            <section>
              <h2>Digest <span className="status">{result.run_mode === "single_agent" ? "Single agent" : "Four roles"} · {result.status}</span></h2>
              <ol>{result.picks.map((s, i) => <StoryCard key={s.url} story={s} n={i + 1} />)}</ol>
              <div className="bar"><button className="secondary" onClick={copyDigest}>{copied ? "Copied" : "Copy as Markdown"}</button></div>
              {result.rejection_log.length > 0 && (
                <><h3>Checker rejections</h3><ul>{result.rejection_log.flatMap((r) => r.issues.map((i, k) =>
                  <li key={`${r.attempt}-${k}`}>Attempt {r.attempt}: pick {i.pick ?? "all"}, {i.type} ({i.detail})</li>))}</ul></>
              )}
              {result.doubts.length > 0 && <><h3>Open doubts</h3><ul>{result.doubts.map((d) => <li key={d}>{d}</li>)}</ul></>}
              <details><summary>Full run report</summary><pre>{result.report}</pre></details>
            </section>
          )}
        </>
      )}

      {tab === "dashboard" && (
        <Dashboard events={shown} config={config} prices={prices} setPrices={setPrices}
          title={viewing ? `Past run ${viewing.name}` : "Current run"} />
      )}
      {tab === "logs" && <Logs events={shown} live={active && !viewing} />}
      {tab === "history" && <History items={history} current={viewing?.name ?? null} onLoad={loadRun} onRefresh={refreshHistory} />}
    </main>
  );
}
