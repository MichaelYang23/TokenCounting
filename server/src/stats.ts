import { normalizeModel } from "./pricing.ts";

export interface SummaryRow {
  contributor: string;
  verified: number;
  date: string;
  primary_model: string;
  effort: string | null;
  category: string | null;
  outcome: string;
  turns: number;
  cost_usd: number | null;
}

/** Linear-interpolated percentile, q in [0, 100] (same as tokencount/report.py). */
export function percentile(values: number[], q: number): number | null {
  const xs = [...values].sort((a, b) => a - b);
  if (!xs.length) return null;
  const k = ((xs.length - 1) * q) / 100;
  const lo = Math.floor(k);
  const hi = Math.ceil(k);
  return xs[lo] + (xs[hi] - xs[lo]) * (k - lo);
}

function countBy<T>(items: T[], key: (item: T) => string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const item of items) out[key(item)] = (out[key(item)] ?? 0) + 1;
  return out;
}

const spread = (xs: number[]) => ({
  p10: percentile(xs, 10),
  p50: percentile(xs, 50),
  p90: percentile(xs, 90),
  max: xs.length ? Math.max(...xs) : null,
});

export function summarize(rows: SummaryRow[], priceTable: string) {
  const groups = new Map<string, SummaryRow[]>();
  for (const row of rows) {
    const key = JSON.stringify([normalizeModel(row.primary_model), row.effort ?? "-"]);
    groups.set(key, [...(groups.get(key) ?? []), row]);
  }
  const groupRows = [...groups.entries()]
    .map(([key, members]) => {
      const [model, effort] = JSON.parse(key) as [string, string];
      const costs = members.map((m) => m.cost_usd).filter((c): c is number => c !== null);
      return {
        model,
        effort,
        tasks: members.length,
        cost_usd: spread(costs),
        turns_p50: percentile(members.map((m) => m.turns), 50),
        outcomes: countBy(members, (m) => m.outcome),
      };
    })
    .sort((a, b) => b.tasks - a.tasks || a.model.localeCompare(b.model) || a.effort.localeCompare(b.effort));
  const dates = rows.map((r) => r.date).sort();
  return {
    records: rows.length,
    contributors: new Set(rows.map((r) => r.contributor)).size,
    verified_records: rows.filter((r) => r.verified === 1).length,
    first_date: dates[0] ?? null,
    last_date: dates[dates.length - 1] ?? null,
    price_table: priceTable,
    outcomes: countBy(rows, (r) => r.outcome),
    categories: countBy(rows, (r) => r.category ?? "unlabeled"),
    groups: groupRows,
  };
}
