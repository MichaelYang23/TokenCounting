// HTTP API for the shared dataset. Pure request -> response logic; the Worker
// entry (index.ts) supplies the database, secrets and the GitHub lookup.
//
//   POST /v1/records          submit up to maxBatch records (X-Contributor-Key and/or GitHub Bearer token)
//   GET  /v1/summary          aggregates over published records (?verified=1)
//   GET  /v1/export           published records, paginated (?after=<cursor>&limit=<n>), CC0
//   GET  /v1/health
//   GET  /v1/admin/flagged    (admin) records held for review
//   POST /v1/admin/review     (admin) {contributor, record_id, action: "publish" | "remove"}
//   POST /v1/admin/recheck    (admin) re-evaluate held records against the current tables
import { PriceTable } from "./pricing.ts";
import { summarize, type SummaryRow } from "./stats.ts";
import { TABLES, type TaskRecord, type Tables } from "./tables.ts";
import { evaluate } from "./validate.ts";

export interface Statement {
  bind(...values: unknown[]): Statement;
  first<T = Record<string, unknown>>(): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<{ results: T[] }>;
  run(): Promise<{ meta: { changes?: number } }>;
}
export interface Database {
  prepare(sql: string): Statement;
  batch(statements: Statement[]): Promise<unknown[]>;
}

export interface Limits {
  maxBatch: number;
  verifiedDaily: number;
  anonymousDaily: number;
  ipDaily: number;
}
// Batches stay small enough to validate within the Workers free plan's 10 ms of CPU per request
// (about 140 µs for a 40-call record); the CLI sends 25 records per request.
export const DEFAULT_LIMITS: Limits = { maxBatch: 100, verifiedDaily: 10000, anonymousDaily: 2000, ipDaily: 5000 };

export interface Deps {
  db: Database;
  salt: string;
  adminToken?: string;
  /** GitHub user ID for an OAuth token, or null if GitHub rejects it. */
  githubUserId(token: string): Promise<number | null>;
  now(): Date;
  limits?: Limits;
  tables?: Tables;
}

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Contributor-Key",
  "Access-Control-Max-Age": "86400",
};

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", ...CORS, ...headers },
  });
}

export async function hmacHex(secret: string, message: string): Promise<string> {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
  return Array.from(sig, (b) => b.toString(16).padStart(2, "0")).join("");
}

function sameSecret(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

interface Identity {
  contributor: string;
  verified: boolean;
  ipKey: string | null;
  /** The anonymous identity of a verified contributor's key: records sent before logging in move over. */
  alias: string | null;
}

const KEY_PATTERN = /^[0-9a-f]{32,128}$/;

async function identify(req: Request, deps: Deps): Promise<Identity | Response> {
  const key = req.headers.get("X-Contributor-Key") ?? "";
  const anonymous = KEY_PATTERN.test(key) ? `an_${(await hmacHex(deps.salt, `anon:${key}`)).slice(0, 24)}` : null;
  const auth = req.headers.get("Authorization");
  if (auth) {
    const token = /^Bearer\s+(\S+)$/i.exec(auth)?.[1];
    const id = token ? await deps.githubUserId(token) : null;
    if (id === null) return json(401, { error: "GitHub did not accept this token. Run `tokencount login` again." });
    const contributor = `gh_${(await hmacHex(deps.salt, `github:${id}`)).slice(0, 24)}`;
    return { contributor, verified: true, ipKey: null, alias: anonymous };
  }
  if (!anonymous) return json(400, { error: "Send an X-Contributor-Key header (tokencount does this) or a GitHub token." });
  const ip = req.headers.get("CF-Connecting-IP");
  return {
    contributor: anonymous,
    verified: false,
    ipKey: ip ? `ip_${(await hmacHex(deps.salt, `ip:${ip}`)).slice(0, 24)}` : null,
    alias: null,
  };
}

async function statuses(db: Database, contributor: string, ids: string[]): Promise<Map<string, string>> {
  const found = new Map<string, string>();
  for (let i = 0; i < ids.length; i += 90) {
    const chunk = ids.slice(i, i + 90);
    const { results } = await db
      .prepare(`SELECT record_id, status FROM records WHERE contributor = ? AND record_id IN (${chunk.map(() => "?").join(",")})`)
      .bind(contributor, ...chunk)
      .all<{ record_id: string; status: string }>();
    for (const row of results) found.set(row.record_id, row.status);
  }
  return found;
}

const UPSERT = `
INSERT INTO records (contributor, record_id, verified, status, reasons, received_at, updated_at,
                     date, primary_model, effort, category, outcome, turns, cost_usd, body)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (contributor, record_id) DO UPDATE SET
  verified = excluded.verified, status = excluded.status, reasons = excluded.reasons,
  updated_at = excluded.updated_at, date = excluded.date, primary_model = excluded.primary_model,
  effort = excluded.effort, category = excluded.category, outcome = excluded.outcome,
  turns = excluded.turns, cost_usd = excluded.cost_usd, body = excluded.body`;

const BUMP_QUOTA = `
INSERT INTO quotas (key, day, used) VALUES (?, ?, ?)
ON CONFLICT (key, day) DO UPDATE SET used = used + excluded.used`;

interface Result {
  record_id: string | null;
  status: "published" | "flagged" | "rejected";
  new?: boolean;
  reasons: string[];
}

async function postRecords(req: Request, deps: Deps, tables: Tables, prices: PriceTable): Promise<Response> {
  const limits = deps.limits ?? DEFAULT_LIMITS;
  const who = await identify(req, deps);
  if (who instanceof Response) return who;
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return json(400, { error: "The body must be JSON." });
  }
  const records = (body as { records?: unknown })?.records;
  if (!Array.isArray(records) || records.length === 0) return json(400, { error: 'Expected {"records": [...]}.' });
  if (records.length > limits.maxBatch) return json(413, { error: `Send at most ${limits.maxBatch} records per request.` });

  const now = deps.now();
  const day = now.toISOString().slice(0, 10);
  const quotas: [string, number][] = [[who.contributor, who.verified ? limits.verifiedDaily : limits.anonymousDaily]];
  if (who.ipKey) quotas.push([who.ipKey, limits.ipDaily]);
  for (const [key, limit] of quotas) {
    const row = await deps.db.prepare("SELECT used FROM quotas WHERE key = ? AND day = ?").bind(key, day).first<{ used: number }>();
    if ((row?.used ?? 0) + records.length > limit) {
      return json(429, { error: `Daily limit of ${limit} records reached; try again tomorrow.` });
    }
  }

  const results: Result[] = [];
  const keep: { index: number; record: TaskRecord; status: "published" | "flagged"; reasons: string[]; cost: number | null }[] = [];
  const seen = new Set<string>();
  records.forEach((raw, index) => {
    const verdict = evaluate(raw, tables, prices, day);
    const id = typeof (raw as TaskRecord)?.record_id === "string" ? (raw as TaskRecord).record_id : null;
    if (verdict.status !== "rejected" && id !== null && seen.has(id)) {
      verdict.status = "rejected";
      verdict.reasons = ["record_id appears twice in this request"];
    }
    if (id !== null) seen.add(id);
    results.push({ record_id: id, status: verdict.status, reasons: verdict.reasons });
    if (verdict.status !== "rejected") keep.push({ index, record: raw as TaskRecord, ...verdict, status: verdict.status });
  });

  const ids = keep.map((k) => k.record.record_id);
  const existing = await statuses(deps.db, who.contributor, ids);
  const before = who.alias ? await statuses(deps.db, who.alias, ids) : new Map<string, string>();

  const stamp = now.toISOString();
  const statements: Statement[] = [];
  for (const k of keep) {
    const result = results[k.index];
    const id = k.record.record_id;
    if (existing.get(id) === "removed" || before.get(id) === "removed") {
      Object.assign(result, { status: "rejected", reasons: ["removed by a moderator"] });
      continue;
    }
    result.new = !existing.has(id) && !before.has(id);
    if (before.has(id)) {
      statements.push(deps.db.prepare("DELETE FROM records WHERE contributor = ? AND record_id = ?").bind(who.alias, id));
    }
    statements.push(
      deps.db.prepare(UPSERT).bind(
        who.contributor, k.record.record_id, who.verified ? 1 : 0, k.status, JSON.stringify(k.reasons), stamp, stamp,
        k.record.date, k.record.primary_model, k.record.effort, k.record.task.category, k.record.outcome.label,
        k.record.turns, k.cost, JSON.stringify(k.record),
      ),
    );
  }
  for (const [key] of quotas) statements.push(deps.db.prepare(BUMP_QUOTA).bind(key, day, records.length));
  await deps.db.batch(statements);
  return json(200, { results });
}

async function getSummary(url: URL, deps: Deps, prices: PriceTable): Promise<Response> {
  const verifiedOnly = url.searchParams.get("verified") === "1";
  const { results } = await deps.db
    .prepare(
      `SELECT contributor, verified, date, primary_model, effort, category, outcome, turns, cost_usd
       FROM records WHERE status = 'published'${verifiedOnly ? " AND verified = 1" : ""}`,
    )
    .all<SummaryRow>();
  return json(200, summarize(results, prices.version), { "Cache-Control": "public, max-age=300" });
}

async function getExport(url: URL, deps: Deps): Promise<Response> {
  const after = Math.max(0, Number.parseInt(url.searchParams.get("after") ?? "0", 10) || 0);
  const limit = Math.min(1000, Math.max(1, Number.parseInt(url.searchParams.get("limit") ?? "500", 10) || 500));
  const { results } = await deps.db
    .prepare("SELECT id, contributor, verified, received_at, body FROM records WHERE status = 'published' AND id > ? ORDER BY id LIMIT ?")
    .bind(after, limit)
    .all<{ id: number; contributor: string; verified: number; received_at: string; body: string }>();
  return json(200, {
    license: "CC0-1.0",
    records: results.map((r) => ({
      contributor: r.contributor,
      verified: r.verified === 1,
      received: r.received_at.slice(0, 10),
      record: JSON.parse(r.body),
    })),
    next: results.length === limit ? results[results.length - 1].id : null,
  });
}

async function admin(req: Request, url: URL, deps: Deps, tables: Tables, prices: PriceTable): Promise<Response> {
  const given = req.headers.get("Authorization") ?? "";
  if (!deps.adminToken || !sameSecret(given, `Bearer ${deps.adminToken}`)) return json(401, { error: "Admin only." });
  const stamp = deps.now().toISOString();
  if (url.pathname === "/v1/admin/flagged" && req.method === "GET") {
    const { results } = await deps.db
      .prepare(`SELECT id, contributor, record_id, reasons, received_at, primary_model, cost_usd
                FROM records WHERE status = 'flagged' ORDER BY id LIMIT 200`)
      .all<Record<string, unknown>>();
    return json(200, { records: results.map((r) => ({ ...r, reasons: JSON.parse(String(r.reasons)) })) });
  }
  if (url.pathname === "/v1/admin/review" && req.method === "POST") {
    const body = (await req.json().catch(() => ({}))) as { contributor?: string; record_id?: string; action?: string };
    const status = body.action === "publish" ? "published" : body.action === "remove" ? "removed" : null;
    if (!status || !body.contributor || !body.record_id) {
      return json(400, { error: 'Expected {"contributor", "record_id", "action": "publish" | "remove"}.' });
    }
    const res = await deps.db
      .prepare("UPDATE records SET status = ?, updated_at = ? WHERE contributor = ? AND record_id = ?")
      .bind(status, stamp, body.contributor, body.record_id)
      .run();
    return json(200, { changed: res.meta.changes ?? 0 });
  }
  if (url.pathname === "/v1/admin/recheck" && req.method === "POST") {
    const { results } = await deps.db
      .prepare("SELECT contributor, record_id, body FROM records WHERE status = 'flagged'")
      .all<{ contributor: string; record_id: string; body: string }>();
    const statements: Statement[] = [];
    let published = 0;
    for (const row of results) {
      const verdict = evaluate(JSON.parse(row.body), tables, prices, stamp.slice(0, 10));
      if (verdict.status === "rejected") continue;
      if (verdict.status === "published") published++;
      statements.push(
        deps.db
          .prepare("UPDATE records SET status = ?, reasons = ?, cost_usd = ?, updated_at = ? WHERE contributor = ? AND record_id = ?")
          .bind(verdict.status, JSON.stringify(verdict.reasons), verdict.cost, stamp, row.contributor, row.record_id),
      );
    }
    if (statements.length) await deps.db.batch(statements);
    return json(200, { rechecked: results.length, published });
  }
  return json(404, { error: "Not found." });
}

export async function handle(req: Request, deps: Deps): Promise<Response> {
  const url = new URL(req.url);
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  const tables = deps.tables ?? TABLES;
  const prices = new PriceTable(tables.prices);
  try {
    if (url.pathname === "/v1/health" && req.method === "GET") {
      return json(200, { ok: true, schema: "0.1", prices: tables.prices.version, policy: tables.policy.version });
    }
    if (url.pathname === "/v1/records" && req.method === "POST") return await postRecords(req, deps, tables, prices);
    if (url.pathname === "/v1/summary" && req.method === "GET") return await getSummary(url, deps, prices);
    if (url.pathname === "/v1/export" && req.method === "GET") return await getExport(url, deps);
    if (url.pathname.startsWith("/v1/admin/")) return await admin(req, url, deps, tables, prices);
    return json(404, { error: "Not found." });
  } catch (err) {
    console.error(err);
    return json(500, { error: "Internal error." });
  }
}
