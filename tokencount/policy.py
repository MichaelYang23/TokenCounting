"""Checks a record must pass before it is shared.

The API (server/src/validate.ts) applies the same rules from the same
policy.json, so problems show up locally before anything is sent:

- errors: the server would reject the record;
- flags: the server would store it but hold it back from public statistics
  until it is reviewed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from .pricing import PriceTable
from .schema import load_schema, validate_record

POLICY_PATH = Path(__file__).with_name("policy.json")


@dataclass
class Check:
    errors: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@lru_cache(maxsize=4)
def load_policy(path: str | None = None) -> dict:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


@lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def _context(call: dict) -> int:
    return call["input_fresh"] + call["cache_write_5m"] + call["cache_write_1h"] + call["cache_read"]


def screen(record: dict, policy: dict) -> list[str]:
    """Personal or secret data in the free-text fields."""
    task = record.get("task") or {}
    problems = []
    for rule in policy["screening"]:
        for name in rule["fields"]:
            text = task.get(name)
            if isinstance(text, str) and _compiled(rule["pattern"]).search(text):
                problems.append(f"{name} contains {rule['name']}")
    return problems


def consistency(record: dict) -> list[str]:
    """Summary fields must agree with the calls they summarize."""
    calls = record["calls"]
    main = [c for c in calls if c["role"] == "main"]
    errors = []
    if record["turns"] != len(main):
        errors.append("turns does not match the number of main-thread calls")
    if record["subagent_calls"] != len(calls) - len(main):
        errors.append("subagent_calls does not match the number of sub-agent calls")
    if record["peak_context_tokens"] != max((_context(c) for c in main), default=0):
        errors.append("peak_context_tokens does not match the calls")
    for i, c in enumerate(calls):
        if c["thinking"] is not None and c["thinking"] > c["output"]:
            errors.append(f"calls[{i}]: thinking exceeds output")
    return errors


def limits(record: dict, policy: dict, today: date) -> list[str]:
    """Values no real task can have."""
    lim = policy["limits"]
    errors = []
    if len(record["calls"]) > lim["max_calls_per_task"]:
        errors.append(f"more than {lim['max_calls_per_task']} calls in one task")
    for i, c in enumerate(record["calls"]):
        if _context(c) > lim["max_context_tokens_per_call"]:
            errors.append(f"calls[{i}]: more input tokens than any model accepts")
        if c["output"] > lim["max_output_tokens_per_call"]:
            errors.append(f"calls[{i}]: more output tokens than any model produces")
    if record["date"] < lim["earliest_date"]:
        errors.append(f"date is before {lim['earliest_date']}")
    if record["date"] > (today + timedelta(days=1)).isoformat():
        errors.append("date is in the future")
    return errors


def check_record(
    record: dict,
    policy: dict | None = None,
    prices: PriceTable | None = None,
    schema: dict | None = None,
    today: date | None = None,
) -> Check:
    policy = policy or load_policy()
    errors = validate_record(record, schema or load_schema())
    if errors:
        return Check(errors=errors[:5])
    errors = consistency(record) + limits(record, policy, today or date.today()) + screen(record, policy)
    if errors:
        return Check(errors=errors)
    flags = []
    cost, unpriced = (prices or PriceTable.load()).record_cost(record)
    if unpriced:
        flags.append("no price for " + ", ".join(sorted(unpriced)))
    if cost > policy["flags"]["cost_usd_above"]:
        flags.append(f"costs more than ${policy['flags']['cost_usd_above']:,}")
    if record["turns"] > policy["flags"]["turns_above"]:
        flags.append(f"more than {policy['flags']['turns_above']} turns")
    return Check(flags=flags)
