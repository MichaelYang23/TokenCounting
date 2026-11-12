// The schema, price table and sharing policy live once, in the Python package,
// and are bundled into the Worker from there so the CLI and the API never drift.
import schema from "../../tokencount/schemas/task_record.v0.1.json" with { type: "json" };
import prices from "../../tokencount/prices.json" with { type: "json" };
import policy from "../../tokencount/policy.json" with { type: "json" };

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Call {
  role: "main" | "subagent";
  model: string;
  effort: string | null;
  speed: string | null;
  inference_geo: string | null;
  input_fresh: number;
  cache_write_5m: number;
  cache_write_1h: number;
  cache_read: number;
  output: number;
  thinking: number | null;
}

export interface TaskRecord {
  schema_version: "0.1";
  record_id: string;
  date: string;
  primary_model: string;
  effort: string | null;
  turns: number;
  subagent_calls: number;
  peak_context_tokens: number;
  server_tools: { web_search_requests: number };
  calls: Call[];
  task: { kind: string; category: string | null; description: string | null; prompt_chars: number | null; prompt: string | null };
  outcome: { label: "success" | "partial" | "fail" | "unknown"; interrupted: boolean };
  [key: string]: unknown;
}

export interface PriceData {
  version: string;
  inference_geo_multipliers: Record<string, number>;
  server_tools: { web_search_per_1000: number };
  models: Record<string, Record<string, unknown>>;
}

export interface Policy {
  version: string;
  limits: {
    earliest_date: string;
    max_calls_per_task: number;
    max_context_tokens_per_call: number;
    max_output_tokens_per_call: number;
  };
  flags: { cost_usd_above: number; turns_above: number };
  screening: { name: string; fields: ("description" | "prompt")[]; pattern: string }[];
}

export interface Tables {
  schema: Json;
  prices: PriceData;
  policy: Policy;
}

export const TABLES: Tables = {
  schema: schema as Json,
  prices: prices as unknown as PriceData,
  policy: policy as unknown as Policy,
};
