import { useEffect, useState } from "react";
import { marked } from "marked";
import { api, type Me, type DbObject, type Column, type QueryResult, type GeniePoll, type GenieScope, type HistoryRow, type SavedRow } from "./api";

type Tab = "explorer" | "genie" | "sql" | "history";

// ---- shared helpers ---------------------------------------------------------
const isNum = (v: any) => v !== null && v !== "" && !isNaN(Number(v));

// Distinct tables referenced by a SQL statement (from FROM / JOIN clauses).
function refsFromSql(sql: string): string[] {
  const m = [...(sql || "").matchAll(/(?:from|join)\s+([`\w.]+)/gi)].map(x => x[1].replace(/`/g, ""));
  return [...new Set(m)].filter(t => t.includes("."));
}

// Compact dependency-free bar chart: first numeric column as value, a non-numeric as label.
function Chart({ columns, rows }: { columns: { name: string }[]; rows: any[][] }) {
  if (!columns?.length || !rows?.length) return null;
  const valueIdx = columns.findIndex((_, i) => rows.every(r => isNum(r[i])));
  if (valueIdx < 0) return null;
  let labelIdx = columns.findIndex((_, i) => i !== valueIdx && !rows.every(r => isNum(r[i])));
  if (labelIdx < 0) labelIdx = valueIdx === 0 ? 1 : 0;
  const data = rows.slice(0, 12).map(r => ({ label: String(r[labelIdx] ?? ""), value: Number(r[valueIdx]) || 0 }));
  const max = Math.max(...data.map(d => d.value), 1);
  return (
    <div className="chart">
      <div className="chart-title">{columns[valueIdx].name}{columns[labelIdx] ? ` by ${columns[labelIdx].name}` : ""}</div>
      {data.map((d, i) => (
        <div key={i} className="bar-row">
          <span className="bar-label" title={d.label}>{d.label}</span>
          <span className="bar-track"><span className="bar-fill" style={{ width: `${(d.value / max) * 100}%` }} /></span>
          <span className="bar-val">{d.value.toLocaleString()}</span>
        </div>
      ))}
    </div>
  );
}

function ResultView({ columns, rows, truncated }: { columns: { name: string; type?: string }[]; rows: any[][]; truncated?: boolean }) {
  if (!columns?.length) return null;
  return (
    <>
      <Chart columns={columns} rows={rows} />
      <div className="results">
        <table>
          <thead><tr>{columns.map(c => <th key={c.name}>{c.name}{c.type && <span className="th-type">{c.type}</span>}</th>)}</tr></thead>
          <tbody>
            {rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className={isNum(v) ? "mono num" : "mono"}>{v === null ? "∅" : String(v)}</td>)}</tr>)}
          </tbody>
        </table>
        {truncated && <div className="dim" style={{ padding: "6px 10px" }}>results truncated</div>}
      </div>
    </>
  );
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [tab, setTab] = useState<Tab>("genie");
  const [sql, setSql] = useState<string>("");

  useEffect(() => {
    api.me().then((m) => {
      setMe(m);
      setSql((prev) => prev ||
        "SELECT `Employee`, `Team`, MEASURE(`High Risk Event Count`) AS high_risk\n" +
        `FROM ${m.catalog}.${m.schema}.mv_risk_behavior\n` +
        "WHERE `Event Date` >= date_sub(current_date(), 7)\nGROUP BY `Employee`, `Team`\nORDER BY high_risk DESC LIMIT 10");
    }).catch(() => {});
  }, []);

  const fq = me ? `${me.catalog}.${me.schema}` : "";
  const useSql = (s: string) => { setSql(s); setTab("sql"); };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">🛡️ RD Security Investigation
          <span className="sub">Secure Statement Execution · Metric Views · Genie · OBO</span>
        </div>
        <div className="who">
          {me ? (
            <>
              <span className="email">{me.email}</span>
              <span className={"badge " + (me.auth_source === "obo" ? "ok" : "warn")}>{me.auth_source}</span>
              <span className="scope">{me.catalog}.{me.schema}</span>
            </>
          ) : <span className="email">…</span>}
        </div>
      </header>

      <nav className="tabs">
        <button className={tab === "genie" ? "on" : ""} onClick={() => setTab("genie")}>Ask Genie</button>
        <button className={tab === "explorer" ? "on" : ""} onClick={() => setTab("explorer")}>Data Explorer</button>
        <button className={tab === "sql" ? "on" : ""} onClick={() => setTab("sql")}>SQL Runner</button>
        <button className={tab === "history" ? "on" : ""} onClick={() => setTab("history")}>History</button>
      </nav>

      <main>
        {tab === "genie" && <Agent onUseSql={useSql} />}
        {tab === "explorer" && <Explorer onUseSql={useSql} fq={fq} />}
        {tab === "sql" && <SqlRunner sql={sql} setSql={setSql} me={me} />}
        {tab === "history" && <History onUseSql={useSql} />}
      </main>
    </div>
  );
}

// --------------------------------------------------------------------- Explorer
function Explorer({ onUseSql, fq }: { onUseSql: (s: string) => void; fq: string }) {
  const [objects, setObjects] = useState<DbObject[]>([]);
  const [sel, setSel] = useState<string | null>(null);
  const [cols, setCols] = useState<Column[]>([]);
  const [err, setErr] = useState<string>("");

  useEffect(() => { api.objects().then(r => setObjects(r.objects)).catch(e => setErr(e.message)); }, []);
  useEffect(() => {
    if (!sel) return;
    setCols([]);
    api.describe(sel).then(r => setCols(r.columns)).catch(e => setErr(e.message));
  }, [sel]);

  return (
    <div className="explorer">
      <div className="obj-list">
        <h3>Objects you can access</h3>
        {err && <div className="error">{err}</div>}
        {objects.map(o => (
          <div key={o.name} className={"obj " + (sel === o.name ? "sel" : "")} onClick={() => setSel(o.name)}>
            <div className="obj-head">
              <span className="obj-name">{o.name}</span>
              <span className={"type " + (o.type === "METRIC_VIEW" ? "mv" : "")}>{o.type}</span>
            </div>
            {o.comment && <div className="obj-comment">{o.comment}</div>}
          </div>
        ))}
      </div>
      <div className="obj-detail">
        {sel ? (
          <>
            <div className="detail-head">
              <h3>{sel}</h3>
              <button onClick={() => onUseSql(`SELECT * FROM ${fq}.${sel} LIMIT 100`)}>
                Query this →
              </button>
            </div>
            <table className="cols">
              <thead><tr><th>Column</th><th>Type</th><th>Tags</th><th>Description</th></tr></thead>
              <tbody>
                {cols.map(c => (
                  <tr key={c.name}>
                    <td className="mono">{c.name}</td>
                    <td className="mono dim">{c.type}</td>
                    <td>{c.tags.map(t => <span key={t} className="tag">{t}</span>)}</td>
                    <td className="dim">{c.comment}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : <div className="hint">Select an object to see its columns, governed tags, and descriptions.</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------ Agent
function Agent({ onUseSql }: { onUseSql: (s: string) => void }) {
  const [q, setQ] = useState("I want to analyze this week's high-risk engineers — which tables and columns should I use?");
  const [scope, setScope] = useState<GenieScope>("security");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [poll, setPoll] = useState<GeniePoll | null>(null);
  const [err, setErr] = useState("");
  // Discovery-first prompts: "what should I use to analyze X" + the scenario.
  const samples = [
    "I want to find this week's high-risk engineers — which tables/columns should I use, and the SQL?",
    "How do I detect data exfiltration to personal cloud / USB? Which columns and what SQL?",
    "What tables show after-hours access to restricted assets, and how would I query it by team?",
    "Which contractors look highest-risk this month — recommend the tables, columns and a query.",
  ];

  async function ask() {
    setErr(""); setPoll(null); setBusy(true); setStage("sending…");
    try {
      const a = await api.genieAsk(q, scope);
      let p = await api.geniePoll(a.conversation_id, a.response_id, scope);
      let tries = 0;
      while (p.status !== "completed" && p.status !== "failed" && tries < 40) {
        setStage((p.progress_steps && p.progress_steps.length ? p.progress_steps[p.progress_steps.length - 1] : "working") + " …");
        await new Promise(r => setTimeout(r, 3000));
        p = await api.geniePoll(a.conversation_id, a.response_id, scope);
        tries++;
      }
      setPoll(p);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); setStage(""); }
  }

  const suggested = poll?.query_items?.length ? poll.query_items[poll.query_items.length - 1].sql : null;
  const tables = suggested ? refsFromSql(suggested) : [];

  return (
    <div className="agent">
      <div className="scope-row">
        <span className="scope-label">Data scope</span>
        <button className={"seg " + (scope === "security" ? "on" : "")} onClick={() => setScope("security")}>Security data · fast</button>
        <button className={"seg " + (scope === "workspace" ? "on" : "")} onClick={() => setScope("workspace")}>Whole workspace · broad</button>
        <span className="dim scope-hint">{scope === "security" ? "curated Genie space (scoped, quick)" : "Genie One — any data you can access (slower)"}</span>
      </div>
      <div className="ask-box">
        <textarea value={q} onChange={e => setQ(e.target.value)} rows={2}
          placeholder="Describe what you want to analyze — Genie recommends the tables/columns + a SQL statement…" />
        <button onClick={ask} disabled={busy}>{busy ? "Genie is thinking…" : "Ask Genie"}</button>
      </div>
      <div className="samples">
        {samples.map(s => <button key={s} className="chip" onClick={() => setQ(s)}>{s}</button>)}
      </div>
      {busy && <div className="stage">⏳ {stage || "working"} <span className="dim">({scope === "security" ? "scoped — usually a few seconds" : "workspace-wide — can take up to a minute"})</span></div>}
      {err && <div className="error">{err}</div>}
      {poll && (
        <div className="answer">
          {suggested ? (
            <div className="suggested">
              <div className="suggested-head">Recommended tables &amp; columns</div>
              <div className="ref-tables">
                {tables.length ? tables.map(t => <span key={t} className="tag">{t}</span>) : <span className="dim">see SQL below</span>}
              </div>
              <div className="suggested-head" style={{ marginTop: 12 }}>Reference SQL <span className="dim">(governed — runs as you)</span></div>
              <pre className="sql">{suggested}</pre>
              <div className="row">
                <button onClick={() => onUseSql(suggested)}>Run in SQL Runner →</button>
                <button className="ghost" onClick={() => navigator.clipboard.writeText(suggested)}>Copy SQL</button>
              </div>
              {poll.columns && poll.rows && poll.rows.length > 0 &&
                <div style={{ marginTop: 14 }}><ResultView columns={poll.columns} rows={poll.rows} /></div>}
            </div>
          ) : <div className="hint">No SQL suggested — try rephrasing, or switch scope.</div>}
          {poll.final_answer &&
            <details className="narrative"><summary>Genie's explanation</summary>
              <div className="md" dangerouslySetInnerHTML={{ __html: marked.parse(poll.final_answer) as string }} />
            </details>}
          {poll.deep_link && <a className="deep" href={poll.deep_link} target="_blank">Open in Databricks ↗</a>}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------------------- History
function History({ onUseSql }: { onUseSql: (s: string) => void }) {
  const [hist, setHist] = useState<HistoryRow[]>([]);
  const [saved, setSaved] = useState<SavedRow[]>([]);
  const [lb, setLb] = useState(true);

  useEffect(() => {
    api.history().then(r => { setHist(r.history); setLb(r.lakebase); }).catch(() => {});
    api.savedList().then(r => setSaved(r.saved)).catch(() => {});
  }, []);

  return (
    <div className="history">
      {!lb && <div className="error">Lakebase not configured — history/saved are disabled. (App still works.)</div>}
      <div className="hist-grid">
        <div>
          <h3>Recent queries <span className="dim">(from Lakebase — instant, no system-table lag)</span></h3>
          {hist.length === 0 && <div className="hint">No queries yet. Run one in SQL Runner.</div>}
          {hist.map((h, i) => (
            <div key={i} className="hist-row" onClick={() => onUseSql(h.sql)}>
              <div className="hist-meta">
                <span className={"pill " + (h.status === "ok" ? "ok" : "bad")}>{h.status}</span>
                <span className="pill src">{h.source}</span>
                <span className="dim">{h.row_count ?? "—"} rows</span>
                <span className="dim time">{new Date(h.created_at).toLocaleString()}</span>
              </div>
              <pre className="hist-sql">{h.sql}</pre>
            </div>
          ))}
        </div>
        <div>
          <h3>Saved queries</h3>
          {saved.length === 0 && <div className="hint">Save a query from SQL Runner with ★ Save.</div>}
          {saved.map(s => (
            <div key={s.id} className="hist-row" onClick={() => onUseSql(s.sql)}>
              <div className="hist-meta"><span className="saved-title">{s.title}</span></div>
              <pre className="hist-sql">{s.sql}</pre>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------- SqlRunner
function SqlRunner({ sql, setSql, me }: { sql: string; setSql: (s: string) => void; me: Me | null }) {
  const [res, setRes] = useState<QueryResult | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [showApi, setShowApi] = useState(false);
  const [tokenMsg, setTokenMsg] = useState("");

  async function run() {
    setErr(""); setRes(null); setBusy(true);
    try { setRes(await api.query(sql)); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }

  async function copyToken() {
    try {
      const t = await api.token();
      await navigator.clipboard.writeText(t.token);
      setTokenMsg("✓ your access token is on the clipboard — paste it as $DATABRICKS_TOKEN (short-lived)");
      setTimeout(() => setTokenMsg(""), 6000);
    } catch (e: any) { setTokenMsg("could not get token: " + e.message); }
  }

  const origin = window.location.origin;
  const curl = `curl -X POST ${origin}/api/query \\\n` +
    `  -H "Authorization: Bearer $DATABRICKS_TOKEN" \\\n` +
    `  -H "Content-Type: application/json" \\\n` +
    `  -d '${JSON.stringify({ sql })}'`;
  const py = `import os, requests\n` +
    `r = requests.post("${origin}/api/query",\n` +
    `    headers={"Authorization": f"Bearer {os.environ['DATABRICKS_TOKEN']}"},\n` +
    `    json={"sql": ${JSON.stringify(sql)}})\n` +
    `print(r.json())`;

  return (
    <div className="runner">
      <textarea className="sql-edit" value={sql} onChange={e => setSql(e.target.value)} rows={8} spellCheck={false} />
      <div className="row">
        <button onClick={run} disabled={busy || !sql.trim()}>{busy ? "Running…" : "Run (as you, OBO)"}</button>
        <button className="ghost" onClick={() => setShowApi(!showApi)}>{showApi ? "Hide" : "Copy as API call"}</button>
        <button className="ghost" onClick={async () => {
          const t = window.prompt("Save this query as:", "My query");
          if (t) { try { await api.save(t, sql); alert("Saved to History → Saved."); } catch (e: any) { alert(e.message); } }
        }}>★ Save</button>
        {res && <span className="dim">↳ {res.row_count} rows{res.truncated ? " (truncated)" : ""} · as {res.executed_as}</span>}
      </div>
      {showApi && (
        <div className="apicall">
          <p className="dim">Same governed execution path — call it directly with your own Databricks token.
            UC enforces your permissions, masks, and row filters. Use <b>Copy token</b> to get a ready-to-use
            <code> $DATABRICKS_TOKEN</code>.</p>
          <div className="row">
            <button onClick={copyToken}>🔑 Copy token</button>
            {tokenMsg && <span className="dim">{tokenMsg}</span>}
          </div>
          <div className="snip"><span>curl</span><button onClick={() => navigator.clipboard.writeText(curl)}>copy</button></div>
          <pre>{curl}</pre>
          <div className="snip"><span>python</span><button onClick={() => navigator.clipboard.writeText(py)}>copy</button></div>
          <pre>{py}</pre>
        </div>
      )}
      {err && <div className="error">{err}</div>}
      {res && <ResultView columns={res.columns} rows={res.rows} truncated={res.truncated} />}
    </div>
  );
}
