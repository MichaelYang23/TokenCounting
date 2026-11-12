// Port of tokencount/pricing.py: the same table, the same rules, the same numbers.
import type { Call, PriceData, TaskRecord } from "./tables.ts";

const RATE_FIELDS = ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"] as const;
type Rates = Record<(typeof RATE_FIELDS)[number], number>;

/** claude-sonnet-4-5-20250929, us.anthropic.claude-sonnet-4-5-20250929-v1:0 and friends -> claude-sonnet-4-5 */
export function normalizeModel(model: string): string {
  let m = model.trim().toLowerCase().split("[")[0].split("@")[0];
  const at = m.indexOf("anthropic.");
  if (at >= 0) m = m.slice(at + "anthropic.".length);
  m = m.replace(/-v\d+(:\d+)?$/, "").replace(/-\d{8}$/, "");
  if (m.endsWith("-latest")) m = m.slice(0, -"-latest".length);
  return m;
}

export class PriceTable {
  readonly version: string;
  private readonly models = new Map<string, Record<string, unknown>>();
  private readonly geo: Record<string, number>;
  private readonly webSearchPrice: number;

  constructor(data: PriceData) {
    this.version = data.version;
    for (const [id, entry] of Object.entries(data.models)) this.models.set(normalizeModel(id), entry);
    this.geo = data.inference_geo_multipliers ?? {};
    this.webSearchPrice = (data.server_tools?.web_search_per_1000 ?? 0) / 1000;
  }

  rates(model: string, speed: string | null, contextTokens: number, inferenceGeo: string | null): Rates | null {
    const entry = this.models.get(normalizeModel(model));
    if (!entry) return null;
    let tier = (speed === "fast" ? entry.fast : entry.standard) as Record<string, number> | undefined;
    if (!tier) return null;
    const long = entry.long_context as Record<string, number> | undefined;
    if (speed !== "fast" && long && contextTokens > long.threshold_tokens) tier = long;
    const factor = this.geo[inferenceGeo ?? ""] ?? 1;
    const rates = {} as Rates;
    for (const f of RATE_FIELDS) rates[f] = Number(tier[f]) * factor;
    return rates;
  }

  /** USD for one call, or null when the table has no price for it. */
  callCost(call: Call): number | null {
    const context = call.input_fresh + call.cache_write_5m + call.cache_write_1h + call.cache_read;
    const r = this.rates(call.model, call.speed, context, call.inference_geo);
    if (!r) return null;
    const thinking = Math.min(call.thinking ?? 0, call.output);
    return (
      (call.input_fresh * r.input +
        call.cache_write_5m * r.cache_write_5m +
        call.cache_write_1h * r.cache_write_1h +
        call.cache_read * r.cache_read +
        (call.output - thinking) * r.output +
        thinking * r.output) /
      1e6
    );
  }

  /** USD cost of a record's priced calls plus web searches, and the models without a price. */
  recordCost(record: TaskRecord): { cost: number; unpriced: string[] } {
    let cost = record.server_tools.web_search_requests * this.webSearchPrice;
    const unpriced = new Set<string>();
    for (const call of record.calls) {
      const c = this.callCost(call);
      if (c === null) unpriced.add(call.model);
      else cost += c;
    }
    return { cost, unpriced: [...unpriced].sort() };
  }
}
