// Port of tokencount/schema.py and tokencount/policy.py. A record ends up:
//   rejected  - fails the schema, consistency, hard limits or screening; not stored
//   flagged   - stored, but kept out of public statistics until reviewed
//   published - everything else
import type { Call, Json, Policy, TaskRecord, Tables } from "./tables.ts";
import { PriceTable } from "./pricing.ts";

export type Status = "published" | "flagged" | "rejected";
export interface Verdict {
  status: Status;
  reasons: string[];
  cost: number | null;
}

type Schema = { [key: string]: Json };

function typeOk(value: unknown, name: string): boolean {
  switch (name) {
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "object":
      return typeof value === "object" && value !== null && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "null":
      return value === null;
    default:
      return typeof value === name;
  }
}

const show = (v: unknown) => JSON.stringify(v);

/** The JSON Schema subset our schema uses: type, const, enum, required, properties,
 * additionalProperties, items, minItems, minimum, maxLength, pattern. */
export function schemaErrors(value: unknown, schema: Schema, path = "$"): string[] {
  const errors: string[] = [];
  if ("const" in schema && value !== schema.const) errors.push(`${path}: expected ${show(schema.const)}, got ${show(value)}`);
  if (Array.isArray(schema.enum) && !schema.enum.some((e) => e === value)) {
    errors.push(`${path}: ${show(value)} is not one of ${show(schema.enum)}`);
  }
  if (schema.type !== undefined) {
    const types = (Array.isArray(schema.type) ? schema.type : [schema.type]) as string[];
    if (!types.some((t) => typeOk(value, t))) {
      errors.push(`${path}: expected ${types.join(" or ")}, got ${value === null ? "null" : Array.isArray(value) ? "array" : typeof value}`);
      return errors;
    }
  }
  if (typeof value === "number" && typeof schema.minimum === "number" && value < schema.minimum) {
    errors.push(`${path}: ${value} is below the minimum ${schema.minimum}`);
  }
  if (typeof value === "string") {
    if (typeof schema.pattern === "string" && !new RegExp(schema.pattern).test(value)) {
      errors.push(`${path}: ${show(value)} does not match ${schema.pattern}`);
    }
    if (typeof schema.maxLength === "number" && [...value].length > schema.maxLength) {
      errors.push(`${path}: longer than ${schema.maxLength} characters`);
    }
  }
  if (typeOk(value, "object")) {
    const obj = value as Record<string, unknown>;
    for (const key of (schema.required as string[] | undefined) ?? []) {
      if (!(key in obj)) errors.push(`${path}: missing field ${show(key)}`);
    }
    const props = (schema.properties as Record<string, Schema> | undefined) ?? {};
    const extra = schema.additionalProperties;
    for (const [key, item] of Object.entries(obj)) {
      if (key in props) errors.push(...schemaErrors(item, props[key], `${path}.${key}`));
      else if (extra === false) errors.push(`${path}: unexpected field ${show(key)}`);
      else if (typeOk(extra, "object")) errors.push(...schemaErrors(item, extra as Schema, `${path}.${key}`));
    }
  }
  if (Array.isArray(value)) {
    if (typeof schema.minItems === "number" && value.length < schema.minItems) {
      errors.push(`${path}: needs at least ${schema.minItems} item(s)`);
    }
    if (typeOk(schema.items, "object")) {
      value.forEach((item, i) => errors.push(...schemaErrors(item, schema.items as Schema, `${path}[${i}]`)));
    }
  }
  return errors;
}

const context = (c: Call) => c.input_fresh + c.cache_write_5m + c.cache_write_1h + c.cache_read;

export function consistencyErrors(r: TaskRecord): string[] {
  const main = r.calls.filter((c) => c.role === "main");
  const errors: string[] = [];
  if (r.turns !== main.length) errors.push("turns does not match the number of main-thread calls");
  if (r.subagent_calls !== r.calls.length - main.length) errors.push("subagent_calls does not match the number of sub-agent calls");
  if (r.peak_context_tokens !== Math.max(0, ...main.map(context))) errors.push("peak_context_tokens does not match the calls");
  r.calls.forEach((c, i) => {
    if (c.thinking !== null && c.thinking > c.output) errors.push(`calls[${i}]: thinking exceeds output`);
  });
  return errors;
}

export function limitErrors(r: TaskRecord, policy: Policy, today: string): string[] {
  const lim = policy.limits;
  const errors: string[] = [];
  if (r.calls.length > lim.max_calls_per_task) errors.push(`more than ${lim.max_calls_per_task} calls in one task`);
  r.calls.forEach((c, i) => {
    if (context(c) > lim.max_context_tokens_per_call) errors.push(`calls[${i}]: more input tokens than any model accepts`);
    if (c.output > lim.max_output_tokens_per_call) errors.push(`calls[${i}]: more output tokens than any model produces`);
  });
  if (r.date < lim.earliest_date) errors.push(`date is before ${lim.earliest_date}`);
  const tomorrow = new Date(`${today}T00:00:00Z`);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (r.date > tomorrow.toISOString().slice(0, 10)) errors.push("date is in the future");
  return errors;
}

const compiled = new Map<string, RegExp>();

export function screen(r: Pick<TaskRecord, "task">, policy: Policy): string[] {
  const problems: string[] = [];
  for (const rule of policy.screening) {
    let rx = compiled.get(rule.pattern);
    if (!rx) compiled.set(rule.pattern, (rx = new RegExp(rule.pattern)));
    for (const field of rule.fields) {
      const text = r.task?.[field];
      if (typeof text === "string" && rx.test(text)) problems.push(`${field} contains ${rule.name}`);
    }
  }
  return problems;
}

export function evaluate(record: unknown, tables: Tables, prices: PriceTable, today: string): Verdict {
  const schemaProblems = schemaErrors(record, tables.schema as Schema);
  if (schemaProblems.length) return { status: "rejected", reasons: schemaProblems.slice(0, 5), cost: null };
  const r = record as TaskRecord;
  const errors = [...consistencyErrors(r), ...limitErrors(r, tables.policy, today), ...screen(r, tables.policy)];
  if (errors.length) return { status: "rejected", reasons: errors, cost: null };
  const { cost, unpriced } = prices.recordCost(r);
  const flags: string[] = [];
  if (unpriced.length) flags.push(`no price for ${unpriced.join(", ")}`);
  if (cost > tables.policy.flags.cost_usd_above) flags.push(`costs more than $${tables.policy.flags.cost_usd_above.toLocaleString("en-US")}`);
  if (r.turns > tables.policy.flags.turns_above) flags.push(`more than ${tables.policy.flags.turns_above} turns`);
  return { status: flags.length ? "flagged" : "published", reasons: flags, cost: unpriced.length ? null : cost };
}
