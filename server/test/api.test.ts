import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import { handle, type Deps, type Limits } from "../src/app.ts";
import { TABLES, type TaskRecord } from "../src/tables.ts";
import { memoryD1 } from "./d1.ts";

const RECORDS: TaskRecord[] = readFileSync(new URL("./fixtures/records.jsonl", import.meta.url), "utf8")
  .trim()
  .split("\n")
  .map((line) => JSON.parse(line));
const KEY = "ab".repeat(32);
const ADMIN = "admin-secret";
const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));

let deps: Deps;
beforeEach(() => {
  deps = {
    db: memoryD1(),
    salt: "test-salt",
    adminToken: ADMIN,
    githubUserId: async (token) => (token === "gho_good" ? 42 : null),
    now: () => new Date("2026-11-12T12:00:00Z"),
  };
});

async function call(method: string, path: string, body?: unknown, headers: Record<string, string> = {}, d: Deps = deps) {
  const init: RequestInit = { method, headers: { ...headers } };
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await handle(new Request(`https://api.test${path}`, init), d);
  return { status: res.status, body: res.status === 204 ? null : await res.json(), headers: res.headers };
}

const submit = (records: unknown[], headers: Record<string, string> = { "X-Contributor-Key": KEY }, d?: Deps) =>
  call("POST", "/v1/records", { records }, headers, d);
const admin = { Authorization: `Bearer ${ADMIN}` };

test("anonymous submissions are published, summarized and exported", async () => {
  const res = await submit(RECORDS);
  assert.equal(res.status, 200);
  assert.deepEqual(res.body.results.map((r: { status: string; new: boolean }) => [r.status, r.new]), RECORDS.map(() => ["published", true]));

  const summary = (await call("GET", "/v1/summary")).body;
  assert.equal(summary.records, 4);
  assert.equal(summary.contributors, 1);
  assert.equal(summary.verified_records, 0);
  assert.deepEqual([summary.first_date, summary.last_date], ["2026-11-01", "2026-11-02"]);
  const opus = summary.groups.find((g: { model: string }) => g.model === "claude-opus-5-5");
  assert.equal(opus.tasks, 2);
  assert.ok(Math.abs(opus.cost_usd.max - 0.01926) < 1e-12);
  assert.deepEqual(summary.groups.map((g: { model: string }) => g.model).sort(), ["claude-opus-5-5", "claude-sonnet-4-5", "claude-sonnet-5-5"]);
  assert.deepEqual(summary.categories, { feature: 1, unlabeled: 3 });

  const exported = (await call("GET", "/v1/export?limit=3")).body;
  assert.equal(exported.license, "CC0-1.0");
  assert.equal(exported.records.length, 3);
  assert.match(exported.records[0].contributor, /^an_[0-9a-f]{24}$/);
  assert.deepEqual(exported.records[0].record, RECORDS[0]);
  const rest = (await call("GET", `/v1/export?limit=3&after=${exported.next}`)).body;
  assert.equal(rest.records.length, 1);
  assert.equal(rest.next, null);
});

test("resubmitting updates in place instead of duplicating", async () => {
  await submit(RECORDS);
  const relabeled = clone(RECORDS[1]);
  relabeled.outcome.label = "fail";
  const res = await submit([relabeled]);
  assert.deepEqual(res.body.results, [{ record_id: relabeled.record_id, status: "published", reasons: [], new: false }]);
  const summary = (await call("GET", "/v1/summary")).body;
  assert.equal(summary.records, 4);
  assert.equal(summary.outcomes.fail, 1);
});

test("bad records are rejected individually; duplicates in one request too", async () => {
  const future = clone(RECORDS[0]);
  future.date = "2027-01-01";
  const res = await submit([future, RECORDS[1], RECORDS[1]]);
  assert.deepEqual(res.body.results.map((r: { status: string }) => r.status), ["rejected", "published", "rejected"]);
  assert.deepEqual(res.body.results[0].reasons, ["date is in the future"]);
  assert.deepEqual(res.body.results[2].reasons, ["record_id appears twice in this request"]);
  assert.equal((await call("GET", "/v1/summary")).body.records, 1);
});

test("requests without an identity, JSON or records are refused", async () => {
  assert.equal((await submit(RECORDS, {})).status, 400);
  assert.equal((await submit(RECORDS, { "X-Contributor-Key": "not-hex" })).status, 400);
  assert.equal((await call("POST", "/v1/records", "{nope", { "X-Contributor-Key": KEY })).status, 400);
  assert.equal((await submit([])).status, 400);
  const limits: Limits = { maxBatch: 2, verifiedDaily: 10, anonymousDaily: 10, ipDaily: 10 };
  assert.equal((await submit(RECORDS, undefined, { ...deps, limits })).status, 413);
});

test("GitHub-verified submissions get a separate, verified identity", async () => {
  const res = await submit(RECORDS.slice(0, 1), { Authorization: "Bearer gho_good" });
  assert.equal(res.body.results[0].status, "published");
  const record = (await call("GET", "/v1/export")).body.records[0];
  assert.equal(record.verified, true);
  assert.match(record.contributor, /^gh_[0-9a-f]{24}$/);
  assert.equal((await submit(RECORDS, { Authorization: "Bearer gho_bad" })).status, 401);
  assert.equal((await call("GET", "/v1/summary?verified=1")).body.records, 1);
});

test("logging in moves records sent anonymously to the verified identity", async () => {
  await submit(RECORDS);
  const both = { "X-Contributor-Key": KEY, Authorization: "Bearer gho_good" };
  const res = await submit(RECORDS, both);
  assert.deepEqual(res.body.results.map((r: { status: string; new: boolean }) => [r.status, r.new]), RECORDS.map(() => ["published", false]));
  const summary = (await call("GET", "/v1/summary")).body;
  assert.deepEqual([summary.records, summary.contributors, summary.verified_records], [4, 1, 4]);

  // A moderator's removal follows the record across the move.
  const anon = { "X-Contributor-Key": "cd".repeat(32) };
  await submit([RECORDS[0]], anon);
  const contributor = (await call("GET", "/v1/export")).body.records.find((r: { verified: boolean }) => !r.verified).contributor;
  await call("POST", "/v1/admin/review", { contributor, record_id: RECORDS[0].record_id, action: "remove" }, admin);
  const moved = await submit([RECORDS[0]], { ...anon, Authorization: "Bearer gho_good" });
  assert.deepEqual(moved.body.results[0].reasons, ["removed by a moderator"]);
});

test("daily limits apply per contributor and per IP address", async () => {
  const limits: Limits = { maxBatch: 500, verifiedDaily: 100, anonymousDaily: 5, ipDaily: 6 };
  const d = { ...deps, limits };
  assert.equal((await submit(RECORDS, undefined, d)).status, 200);
  const again = await submit(RECORDS.slice(0, 2), undefined, d);
  assert.equal(again.status, 429);
  assert.match(again.body.error, /Daily limit of 5 records/);
  const otherKey = { "X-Contributor-Key": "cd".repeat(32), "CF-Connecting-IP": "203.0.113.9" };
  assert.equal((await submit(RECORDS, otherKey, d)).status, 200);
  // Same IP, new key: 4 already used + 3 > the IP's 6 per day.
  assert.equal((await submit(RECORDS.slice(0, 3), { ...otherKey, "X-Contributor-Key": "ef".repeat(32) }, d)).status, 429);
  assert.equal((await submit(RECORDS.slice(0, 2), { ...otherKey, "X-Contributor-Key": "ef".repeat(32) }, d)).status, 200);
});

test("flagged records wait for review; removals stick", async () => {
  const odd = clone(RECORDS[0]);
  odd.calls[0].model = "claude-opus-9";
  const res = await submit([odd, RECORDS[1]]);
  assert.deepEqual(res.body.results[0], { record_id: odd.record_id, status: "flagged", reasons: ["no price for claude-opus-9"], new: true });
  assert.equal((await call("GET", "/v1/summary")).body.records, 1);

  assert.equal((await call("GET", "/v1/admin/flagged")).status, 401);
  assert.equal((await call("GET", "/v1/admin/flagged", undefined, { Authorization: "Bearer wrong" })).status, 401);
  const flagged = (await call("GET", "/v1/admin/flagged", undefined, admin)).body.records;
  assert.deepEqual(flagged.map((r: { record_id: string; reasons: string[] }) => [r.record_id, r.reasons]), [[odd.record_id, ["no price for claude-opus-9"]]]);

  // Once the price table knows the model, a recheck publishes it.
  const tables = clone(TABLES);
  tables.prices.models["claude-opus-9"] = tables.prices.models["claude-opus-5-5"];
  const recheck = await call("POST", "/v1/admin/recheck", undefined, admin, { ...deps, tables });
  assert.deepEqual(recheck.body, { rechecked: 1, published: 1 });
  assert.equal((await call("GET", "/v1/summary")).body.records, 2);

  const contributor = (await call("GET", "/v1/export")).body.records[0].contributor;
  const removed = await call("POST", "/v1/admin/review", { contributor, record_id: RECORDS[1].record_id, action: "remove" }, admin);
  assert.deepEqual(removed.body, { changed: 1 });
  const resubmit = await submit([RECORDS[1]]);
  assert.deepEqual(resubmit.body.results[0], { record_id: RECORDS[1].record_id, status: "rejected", reasons: ["removed by a moderator"] });
  assert.equal((await call("GET", "/v1/summary")).body.records, 1);
});

test("health, CORS preflight and unknown routes", async () => {
  const health = await call("GET", "/v1/health");
  assert.deepEqual(health.body, { ok: true, schema: "0.1", prices: TABLES.prices.version, policy: TABLES.policy.version });
  assert.equal(health.headers.get("Access-Control-Allow-Origin"), "*");
  const preflight = await call("OPTIONS", "/v1/records");
  assert.equal(preflight.status, 204);
  assert.match(preflight.headers.get("Access-Control-Allow-Headers") ?? "", /X-Contributor-Key/);
  assert.equal((await call("GET", "/v1/nope")).status, 404);
});
