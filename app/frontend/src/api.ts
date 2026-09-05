// Thin API client for the FastAPI backend. All calls are same-origin; the browser
// session is authenticated by Databricks Apps (OBO), so no token handling here.

export interface Me {
  email: string; auth_source: string; catalog: string; schema: string;
  warehouse_id: string; host: string;
}
export interface DbObject { name: string; type: string; comment: string | null; }
export interface Column { name: string; type: string; comment: string | null; tags: string[]; }
export interface QueryResult {
  columns: { name: string; type: string }[];
  rows: string[][]; row_count: number; truncated: boolean;
  statement_id: string; executed_as?: string;
}
export interface QueryItem { item_id: string; sql: string; }
export interface GenieAsk { conversation_id: string; response_id: string; status: string; scope: string; }
export interface GeniePoll {
  status: string; final_answer: string | null; deep_link: string | null; query_items: QueryItem[];
  progress_steps?: string[];
  columns?: { name: string; type: string }[] | null;
  rows?: string[][] | null;
}
export type GenieScope = "security" | "workspace";
export interface HistoryRow { sql: string; source: string; row_count: number | null; status: string; statement_id: string | null; created_at: string; }
export interface SavedRow { id: number; title: string; sql: string; created_at: string; }

async function j<T>(r: Response): Promise<T> {
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { const b = await r.json(); msg = b.detail || b.error || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

export const api = {
  me: () => fetch("/api/me").then(j<Me>),
  objects: () => fetch("/api/objects").then(j<{ objects: DbObject[] }>),
  describe: (name: string) => fetch(`/api/objects/${encodeURIComponent(name)}`).then(j<{ name: string; columns: Column[] }>),
  query: (sql: string) =>
    fetch("/api/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sql }) }).then(j<QueryResult>),
  genieAsk: (question: string, scope: GenieScope, conversation_id?: string) =>
    fetch("/api/genie/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, scope, conversation_id }) }).then(j<GenieAsk>),
  geniePoll: (conversation_id: string, response_id: string, scope: GenieScope) =>
    fetch(`/api/genie/poll?conversation_id=${conversation_id}&response_id=${response_id}&scope=${scope}`).then(j<GeniePoll>),
  token: () => fetch("/api/token").then(j<{ token: string; email: string; note: string }>),
  history: () => fetch("/api/history").then(j<{ history: HistoryRow[]; lakebase: boolean }>),
  savedList: () => fetch("/api/saved").then(j<{ saved: SavedRow[]; lakebase: boolean }>),
  save: (title: string, sql: string) =>
    fetch("/api/saved", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title, sql }) }).then(j<{ id: number }>),
};
