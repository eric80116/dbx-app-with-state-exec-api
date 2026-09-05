import { useEffect, useState } from "react";
import { marked } from "marked";
import { api, type Me, type DbObject, type Column, type QueryResult, type GeniePoll, type HistoryRow, type SavedRow } from "./api";

type Tab = "explorer" | "genie" | "sql" | "history";

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
  const [q, setQ] = useState("Which engineers showed the most high-risk behavior this week?");
  const [busy, setBusy] = useState(false);
  const [poll, setPoll] = useState<GeniePoll | null>(null);
  const [err, setErr] = useState("");
  const samples = [
    "Which engineers showed the most high-risk behavior this week?",
    "Who downloaded sensitive data to personal cloud or USB in the last 14 days?",
    "Show after-hours access to restricted assets, broken down by team.",
  ];

  async function ask() {
    setErr(""); setPoll(null); setBusy(true);
    try {
      const a = await api.genieAsk(q);
      let p = await api.geniePoll(a.conversation_id, a.response_id);
      let tries = 0;
      while (p.status !== "completed" && p.status !== "failed" && tries < 30) {
        await new Promise(r => setTimeout(r, 3000));
        p = await api.geniePoll(a.conversation_id, a.response_id);
        tries++;
      }
      setPoll(p);
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
  }

  const suggested = poll?.query_items?.length ? poll.query_items[poll.query_items.length - 1].sql : null;

  return (
    <div className="agent">
      <div className="ask-box">
        <textarea value={q} onChange={e => setQ(e.target.value)} rows={2}
          placeholder="Ask about engineer access risk, exfiltration, after-hours activity…" />
        <button onClick={ask} disabled={busy}>{busy ? "Genie is thinking…" : "Ask Genie"}</button>
      </div>
      <div className="samples">
        {samples.map(s => <button key={s} className="chip" onClick={() => setQ(s)}>{s}</button>)}
      </div>
      {err && <div className="error">{err}</div>}
      {poll && (
        <div className="answer">
          {poll.final_answer &&
            <div className="md" dangerouslySetInnerHTML={{ __html: marked.parse(poll.final_answer) as string }} />}
          {suggested && (
            <div className="suggested">
              <div className="suggested-head">Suggested SQL <span className="dim">(runs governed, as you)</span></div>
              <pre className="sql">{suggested}</pre>
              <div className="row">
                <button onClick={() => onUseSql(suggested)}>Run in SQL Runner →</button>
              </div>
            </div>
          )}
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

  async function run() {
    setErr(""); setRes(null); setBusy(true);
    try { setRes(await api.query(sql)); } catch (e: any) { setErr(e.message); } finally { setBusy(false); }
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
        <button onClick={run} disabled={busy}>{busy ? "Running…" : "Run (as you, OBO)"}</button>
        <button className="ghost" onClick={() => setShowApi(!showApi)}>{showApi ? "Hide" : "Copy as API call"}</button>
        <button className="ghost" onClick={async () => {
          const t = window.prompt("Save this query as:", "My query");
          if (t) { try { await api.save(t, sql); alert("Saved to History → Saved."); } catch (e: any) { alert(e.message); } }
        }}>★ Save</button>
        {res && <span className="dim">↳ {res.row_count} rows{res.truncated ? " (truncated)" : ""} · as {res.executed_as}</span>}
      </div>
      {showApi && (
        <div className="apicall">
          <p className="dim">Same governed execution path — call it directly with your own Databricks token
            (App OAuth per-user). UC enforces your permissions, masks, and row filters.</p>
          <div className="snip"><span>curl</span><button onClick={() => navigator.clipboard.writeText(curl)}>copy</button></div>
          <pre>{curl}</pre>
          <div className="snip"><span>python</span><button onClick={() => navigator.clipboard.writeText(py)}>copy</button></div>
          <pre>{py}</pre>
        </div>
      )}
      {err && <div className="error">{err}</div>}
      {res && (
        <div className="results">
          <table>
            <thead><tr>{res.columns.map(c => <th key={c.name}>{c.name}<span className="th-type">{c.type}</span></th>)}</tr></thead>
            <tbody>
              {res.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j} className="mono">{v === null ? "∅" : v}</td>)}</tr>)}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
