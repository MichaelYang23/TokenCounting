"""List prices and the dollar cost of a model call.

Records store token counts, never dollars: prices change, while the token
counts are what actually happened. Cost is derived here from a dated table.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .claude_code import ApiCall

PRICES_PATH = Path(__file__).with_name("prices.json")
RATE_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
COST_PARTS = ("input", "cache_write", "cache_read", "output", "thinking", "web_search")

_DATE_SUFFIX = re.compile(r"-\d{8}$")
_BEDROCK_VERSION = re.compile(r"-v\d+(:\d+)?$")


def normalize_model(model: str) -> str:
    """Map provider-specific spellings to one ID.

    ``claude-sonnet-4-5-20250929``, ``us.anthropic.claude-sonnet-4-5-20250929-v1:0``,
    ``claude-sonnet-4-5@20250929`` and ``claude-sonnet-4-5[1m]`` all become
    ``claude-sonnet-4-5``.
    """
    m = model.strip().lower().split("[", 1)[0].split("@", 1)[0]
    if "anthropic." in m:
        m = m.split("anthropic.", 1)[1]
    m = _BEDROCK_VERSION.sub("", m)
    m = _DATE_SUFFIX.sub("", m)
    if m.endswith("-latest"):
        m = m[: -len("-latest")]
    return m


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float
    output: float

    def scaled(self, factor: float) -> Rates:
        return Rates(*(getattr(self, f) * factor for f in RATE_FIELDS))


class PriceTable:
    def __init__(self, data: dict):
        self.version: str = data.get("version", "unknown")
        self.source: str | None = data.get("source")
        self.models: dict[str, dict] = {normalize_model(k): v for k, v in data.get("models", {}).items()}
        self.geo_multipliers: dict[str, float] = data.get("inference_geo_multipliers", {})
        per_1000 = data.get("server_tools", {}).get("web_search_per_1000", 0.0)
        self.web_search_price: float = float(per_1000) / 1000

    @classmethod
    def load(cls, path: Path | None = None) -> PriceTable:
        return cls(json.loads(Path(path or PRICES_PATH).read_text(encoding="utf-8")))

    def rates(
        self,
        model: str,
        *,
        speed: str | None = None,
        context_tokens: int = 0,
        inference_geo: str | None = None,
    ) -> Rates | None:
        """Rates for one call, or None when the table has no price for it."""
        entry = self.models.get(normalize_model(model))
        if entry is None:
            return None
        tier = entry.get("fast") if speed == "fast" else entry.get("standard")
        if tier is None:
            return None
        long_context = entry.get("long_context")
        if speed != "fast" and long_context and context_tokens > long_context["threshold_tokens"]:
            tier = long_context
        rates = Rates(*(float(tier[f]) for f in RATE_FIELDS))
        factor = float(self.geo_multipliers.get(inference_geo or "", 1.0))
        return rates.scaled(factor) if factor != 1.0 else rates

    def call_cost(self, call: ApiCall) -> dict[str, float] | None:
        """Dollar cost of one call split into COST_PARTS; None if the model is unpriced."""
        r = self.rates(
            call.model,
            speed=call.speed,
            context_tokens=call.context_tokens,
            inference_geo=call.inference_geo,
        )
        if r is None:
            return None
        thinking = min(call.thinking or 0, call.output)
        return {
            "input": call.input_fresh * r.input / 1e6,
            "cache_write": (call.cache_write_5m * r.cache_write_5m + call.cache_write_1h * r.cache_write_1h) / 1e6,
            "cache_read": call.cache_read * r.cache_read / 1e6,
            "output": (call.output - thinking) * r.output / 1e6,
            "thinking": thinking * r.output / 1e6,
            "web_search": call.web_search_requests * self.web_search_price,
        }

    def record_cost(self, record: dict) -> tuple[float, set[str]]:
        """USD cost of a schema-v0.1 record's priced calls, and the models without a price."""
        total, unpriced = 0.0, set()
        for c in record["calls"]:
            call = ApiCall(
                request_id="",
                model=c["model"],
                role=c["role"],
                speed=c["speed"],
                inference_geo=c["inference_geo"],
                input_fresh=c["input_fresh"],
                cache_write_5m=c["cache_write_5m"],
                cache_write_1h=c["cache_write_1h"],
                cache_read=c["cache_read"],
                output=c["output"],
                thinking=c["thinking"],
            )
            cost = self.call_cost(call)
            if cost is None:
                unpriced.add(c["model"])
            else:
                total += sum(cost.values())
        total += record["server_tools"]["web_search_requests"] * self.web_search_price
        return total, unpriced
