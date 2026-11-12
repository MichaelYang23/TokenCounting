"""Summaries of tasks: cost distributions and where the tokens and money went."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Callable, Sequence

from .claude_code import Task
from .pricing import COST_PARTS, PriceTable, normalize_model

FOOTNOTE = (
    "API-equivalent cost at list prices, computed from the usage your logs recorded. Not included:\n"
    "model calls that tools make on their own (Claude Code's WebSearch and WebFetch, for example)."
)


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]."""
    xs = sorted(values)
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def model_name(task: Task) -> str:
    """Primary model with provider/date suffixes removed, so variants group together."""
    return normalize_model(task.primary_model) if task.primary_model else "unknown"


def task_cost(task: Task, prices: PriceTable) -> tuple[float, dict[str, float], int]:
    """(total USD, USD by part, number of calls that could not be priced)."""
    parts: dict[str, float] = defaultdict(float)
    unpriced = 0
    for call in task.calls:
        cost = prices.call_cost(call)
        if cost is None:
            unpriced += 1
            continue
        for name, value in cost.items():
            parts[name] += value
    return sum(parts.values()), dict(parts), unpriced


def _spread(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "p10": percentile(values, 10),
        "p50": percentile(values, 50),
        "p90": percentile(values, 90),
        "max": max(values) if values else None,
    }


def summarize(
    tasks: Sequence[Task],
    prices: PriceTable,
    short_id: Callable[[Task], str],
    top: int = 5,
) -> dict:
    """Everything the report shows, as plain data. Contains no prompt text."""
    costs = {t.key: task_cost(t, prices) for t in tasks}
    totals = [costs[t.key][0] for t in tasks]

    groups: dict[tuple[str, str], list[Task]] = defaultdict(list)
    for t in tasks:
        groups[(model_name(t), t.effort or "-")].append(t)
    group_rows = []
    for (model, effort), members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        group_rows.append(
            {
                "model": model,
                "effort": effort,
                "tasks": len(members),
                **_spread([costs[t.key][0] for t in members]),
                "turns_p50": percentile([len(t.main_calls) for t in members], 50),
            }
        )

    calls = [c for t in tasks for c in t.calls]
    thinking_known = any(c.thinking is not None for c in calls)
    thinking = sum(min(c.thinking or 0, c.output) for c in calls)
    tokens = {
        "input": sum(c.input_fresh for c in calls),
        "cache_write": sum(c.cache_write_5m + c.cache_write_1h for c in calls),
        "cache_read": sum(c.cache_read for c in calls),
        "output": sum(c.output for c in calls) - thinking,
        "thinking": thinking if thinking_known else None,
        "web_search": sum(c.web_search_requests for c in calls),
    }
    money: dict[str, float] = defaultdict(float)
    for _, parts, _ in costs.values():
        for name, value in parts.items():
            money[name] += value
    breakdown = [{"part": p, "tokens": tokens[p], "cost": money.get(p, 0.0)} for p in COST_PARTS]

    unpriced = Counter(c.model for c in calls if prices.call_cost(c) is None)

    ranked = sorted(tasks, key=lambda t: costs[t.key][0], reverse=True)[:top]
    dates = [t.first_seen for t in tasks if t.first_seen]
    return {
        "tasks": len(tasks),
        "sessions": len({t.session_id for t in tasks}),
        "first_date": min(dates).date().isoformat() if dates else None,
        "last_date": max(dates).date().isoformat() if dates else None,
        "price_table": prices.version,
        "total_cost": sum(totals),
        "cost_per_task": _spread(totals),
        "turns": _spread([len(t.main_calls) for t in tasks]),
        "groups": group_rows,
        "breakdown": breakdown,
        "unpriced_calls": dict(unpriced),
        "top": [
            {
                "id": short_id(t),
                "date": t.first_seen.date().isoformat() if t.first_seen else None,
                "model": model_name(t),
                "effort": t.effort,
                "turns": len(t.main_calls),
                "subagent_calls": len(t.subagent_calls),
                "cost": costs[t.key][0],
                "interrupted": t.interrupted,
            }
            for t in ranked
        ],
    }


# --- formatting -----------------------------------------------------------


def fmt_usd(x: float | None) -> str:
    if x is None:
        return "n/a"
    if x >= 100:
        return f"${x:,.0f}"
    if x >= 1:
        return f"${x:.2f}"
    if x >= 0.01:
        return f"${x:.3f}"
    if x == 0:
        return "$0"
    return f"${x:.4f}"


def fmt_tokens(n: float | None) -> str:
    if n is None:
        return "n/a"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    return f"{n:.0f}"


def fmt_num(n: float | None) -> str:
    return "n/a" if n is None else f"{n:.0f}"


def preview(text: str | None, width: int = 48) -> str:
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _table(rows: list[list[str]], right: set[int], indent: str = "  ") -> list[str]:
    """Align columns; the last column is left free so wide (e.g. CJK) text can't break it."""
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    lines = []
    for r in rows:
        cells = [r[i].rjust(w) if i in right else r[i].ljust(w) for i, w in enumerate(widths)]
        lines.append((indent + "  ".join(cells + [r[-1]])).rstrip())
    return lines


_PART_LABELS = {
    "input": "input (uncached)",
    "cache_write": "cache writes",
    "cache_read": "cache reads",
    "output": "output (visible)",
    "thinking": "thinking",
    "web_search": "web searches",
}


def render_report(summary: dict, previews: dict[str, str] | None = None) -> str:
    s = summary
    out = [
        "TokenCounting · Claude Code",
        f"{s['tasks']} task(s) in {s['sessions']} session(s) · {s['first_date']} → {s['last_date']}"
        f" · prices as of {s['price_table']}",
        "",
        "Cost per task (USD)",
    ]
    rows = [["", "tasks", "p10", "p50", "p90", "max", "turns p50", ""]]
    spread = s["cost_per_task"]
    rows.append(
        ["all tasks", str(s["tasks"]), *(fmt_usd(spread[k]) for k in ("p10", "p50", "p90", "max")),
         fmt_num(s["turns"]["p50"]), ""]
    )
    for g in s["groups"]:
        rows.append(
            [f"{g['model']} · {g['effort']}", str(g["tasks"]), *(fmt_usd(g[k]) for k in ("p10", "p50", "p90", "max")),
             fmt_num(g["turns_p50"]), ""]
        )
    out += _table(rows, right={1, 2, 3, 4, 5, 6})

    out += ["", "Where the money went"]
    total_cost = sum(b["cost"] for b in s["breakdown"]) or 0.0
    rows = [["", "tokens", "cost", "share", ""]]
    for b in s["breakdown"]:
        if b["part"] == "web_search" and not b["tokens"]:
            continue
        amount = f"{b['tokens']} searches" if b["part"] == "web_search" else fmt_tokens(b["tokens"])
        share = f"{b['cost'] / total_cost:.0%}" if total_cost else "n/a"
        rows.append([_PART_LABELS[b["part"]], amount, fmt_usd(b["cost"]), share, ""])
    token_total = sum(b["tokens"] or 0 for b in s["breakdown"] if b["part"] != "web_search")
    rows.append(["total", fmt_tokens(token_total), fmt_usd(total_cost), "100%" if total_cost else "n/a", ""])
    out += _table(rows, right={1, 2, 3})

    out += ["", "Most expensive tasks"]
    rows = [["id", "date", "model", "effort", "turns", "sub", "cost", "prompt" if previews is not None else ""]]
    for t in s["top"]:
        note = (previews or {}).get(t["id"], "") if previews is not None else ""
        if t["interrupted"]:
            note = f"(interrupted) {note}".strip()
        rows.append([t["id"], t["date"] or "", t["model"] or "", t["effort"] or "-", str(t["turns"]),
                     str(t["subagent_calls"]), fmt_usd(t["cost"]), note])
    out += _table(rows, right={4, 5, 6})

    if s["unpriced_calls"]:
        listed = ", ".join(f"{m} ({n})" for m, n in sorted(s["unpriced_calls"].items()))
        out += ["", f"Not priced (no entry in prices.json): {listed}"]
    out += ["", FOOTNOTE]
    return "\n".join(out)


def render_tasks(
    tasks: Sequence[Task],
    prices: PriceTable,
    short_id: Callable[[Task], str],
    labels: Callable[[Task], dict],
    show_prompts: bool = True,
) -> str:
    rows = [["id", "date", "model", "effort", "turns", "sub", "cost", "outcome", "prompt" if show_prompts else ""]]
    for t in tasks:
        label = labels(t)
        outcome = label.get("outcome", "-")
        if t.interrupted:
            outcome += "*"
        rows.append(
            [short_id(t), t.first_seen.date().isoformat() if t.first_seen else "", model_name(t),
             t.effort or "-", str(len(t.main_calls)), str(len(t.subagent_calls)),
             fmt_usd(task_cost(t, prices)[0]), outcome, preview(t.prompt_text) if show_prompts else ""]
        )
    lines = _table(rows, right={4, 5, 6})
    lines.append("")
    lines.append("* interrupted by the user. Label a task with: tokencount label <id> --outcome success|partial|fail")
    return "\n".join(lines)
