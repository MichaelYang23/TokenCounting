import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { PriceTable, normalizeModel } from "../src/pricing.ts";
import { percentile } from "../src/stats.ts";
import { TABLES, type TaskRecord } from "../src/tables.ts";
import { evaluate, screen } from "../src/validate.ts";

// Exported by the Python CLI from tests/fixtures.py (`python3 tests/fixtures.py` refreshes it).
const RECORDS: TaskRecord[] = readFileSync(new URL("./fixtures/records.jsonl", import.meta.url), "utf8")
  .trim()
  .split("\n")
  .map((line) => JSON.parse(line));
const CASES = JSON.parse(readFileSync(new URL("../../tests/policy_cases.json", import.meta.url), "utf8"));
const prices = new PriceTable(TABLES.prices);
const TODAY = "2026-11-12";
const clone = (r: TaskRecord): TaskRecord => JSON.parse(JSON.stringify(r));

test("costs match the Python implementation to the micro-dollar", () => {
  // Same hand-computed values as tests/test_tokencount.py.
  const expected = [15040 + 4220, 2616 + 6444 + 540.3 + 107.7, 4090, 1050];
  RECORDS.forEach((record, i) => {
    const verdict = evaluate(record, TABLES, prices, TODAY);
    assert.equal(verdict.status, "published", JSON.stringify(verdict.reasons));
    assert.ok(Math.abs((verdict.cost ?? -1) - expected[i] / 1e6) < 1e-12, `record ${i}: ${verdict.cost}`);
  });
});

test("model IDs normalize like the Python version", () => {
  for (const raw of [
    "claude-sonnet-4-5-20250929",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "claude-sonnet-4-5@20250929",
    "claude-sonnet-4-5[1m]",
    "CLAUDE-SONNET-4-5",
  ]) {
    assert.equal(normalizeModel(raw), "claude-sonnet-4-5", raw);
  }
});

test("Haiku 5.5 long-context rates, fast mode and US inference", () => {
  const call = { role: "main" as const, effort: null, speed: null, inference_geo: null, cache_write_5m: 0, cache_write_1h: 0, thinking: null };
  const small = prices.callCost({ ...call, model: "claude-haiku-5-5", input_fresh: 1000, cache_read: 49_000, output: 1000 });
  const large = prices.callCost({ ...call, model: "claude-haiku-5-5", input_fresh: 1000, cache_read: 150_000, output: 1000 });
  assert.ok(Math.abs(small! - (1000 * 0.1 + 49_000 * 0.01 + 1000 * 0.5) / 1e6) < 1e-15);
  assert.ok(Math.abs(large! - (1000 * 0.5 + 150_000 * 0.05 + 1000 * 2.5) / 1e6) < 1e-15);
  const base = prices.callCost({ ...call, model: "claude-opus-5-5", input_fresh: 1000, cache_read: 1000, output: 0 })!;
  const fast = prices.callCost({ ...call, model: "claude-opus-5-5", input_fresh: 1000, cache_read: 1000, output: 0, speed: "fast" })!;
  const us = prices.callCost({ ...call, model: "claude-opus-5-5", input_fresh: 1000, cache_read: 1000, output: 0, inference_geo: "us" })!;
  assert.ok(Math.abs(fast - 2 * base) < 1e-15 && Math.abs(us - 1.1 * base) < 1e-15);
  assert.equal(prices.callCost({ ...call, model: "claude-sonnet-4-6", input_fresh: 1, cache_read: 0, output: 1, speed: "fast" }), null);
});

test("screening agrees with the CLI on the shared cases", () => {
  for (const [text, name] of CASES.catch as [string, string][]) {
    assert.ok(screen({ task: { description: text } } as TaskRecord, TABLES.policy).includes(`description contains ${name}`), text);
  }
  for (const text of CASES.allow as string[]) {
    assert.deepEqual(screen({ task: { description: text } } as TaskRecord, TABLES.policy), [], text);
  }
  const prompt = { task: { description: null, prompt: "open /home/jane/app.py and https://example.com" } } as unknown as TaskRecord;
  assert.deepEqual(screen(prompt, TABLES.policy), []);
});

test("schema, consistency and limit problems reject a record", () => {
  const missing = clone(RECORDS[0]) as Partial<TaskRecord>;
  delete missing.calls;
  assert.deepEqual(evaluate(missing, TABLES, prices, TODAY).reasons, ["$: missing field \"calls\""]);

  const broken = clone(RECORDS[1]);
  broken.turns += 1;
  broken.calls[0].cache_read = 3_000_000;
  broken.date = "2027-01-01";
  const verdict = evaluate(broken, TABLES, prices, TODAY);
  assert.equal(verdict.status, "rejected");
  for (const reason of [
    "turns does not match the number of main-thread calls",
    "calls[0]: more input tokens than any model accepts",
    "date is in the future",
  ]) {
    assert.ok(verdict.reasons.includes(reason), reason);
  }

  const leaky = clone(RECORDS[0]);
  leaky.task.description = "ask jane@example.com";
  assert.deepEqual(evaluate(leaky, TABLES, prices, TODAY).reasons, ["description contains an e-mail address"]);
});

test("unknown models and very expensive tasks are held for review", () => {
  const odd = clone(RECORDS[1]);
  odd.calls[0].model = "brand-new-model";
  assert.deepEqual(evaluate(odd, TABLES, prices, TODAY), { status: "flagged", reasons: ["no price for brand-new-model"], cost: null });

  const big = { ...RECORDS[1].calls[0], role: "main" as const, model: "claude-opus-5-5", input_fresh: 0, cache_write_5m: 0, cache_write_1h: 1_900_000, cache_read: 0, output: 1000, thinking: 0 };
  const pricey = { ...clone(RECORDS[1]), calls: Array.from({ length: 100 }, () => ({ ...big })), turns: 100, subagent_calls: 0, peak_context_tokens: 1_900_000 };
  const verdict = evaluate(pricey, TABLES, prices, TODAY);
  assert.equal(verdict.status, "flagged");
  assert.deepEqual(verdict.reasons, ["costs more than $1,000"]);
});

test("percentile matches the Python report", () => {
  assert.equal(percentile([3, 1, 2], 50), 2);
  assert.equal(percentile([0, 10], 90), 9);
  assert.equal(percentile([], 50), null);
});
