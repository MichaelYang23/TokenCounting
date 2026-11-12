"""Turn tasks into de-identified, shareable records (schema v0.1).

What leaves your machine by default: token counts per call, model names,
effort levels, tool-call counts, the task date (no time), its duration and
the labels you add yourself. What never does: prompt text (unless you pass
--with-prompt), file paths, project names, session IDs, MCP server names.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections import Counter
from pathlib import Path

from . import __version__
from .claude_code import HARNESS, Task

SCHEMA_VERSION = "0.1"
OUTCOMES = ("success", "partial", "fail", "unknown")
CATEGORIES = ("feature", "bugfix", "refactor", "review", "explain", "research", "writing", "data", "ops", "other")

# Built-in tool names that are safe to publish. MCP tools are reported as
# "mcp" and anything else as "other", so private server or tool names stay local.
SHAREABLE_TOOLS = frozenset(
    {
        "Agent", "Task", "Bash", "BashOutput", "KillBash", "KillShell", "Read", "Write", "Edit",
        "MultiEdit", "NotebookEdit", "NotebookRead", "Glob", "Grep", "LS", "WebFetch", "WebSearch",
        "TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskStop",
        "TaskOutput", "ExitPlanMode", "EnterPlanMode", "Skill", "SlashCommand", "ToolSearch",
        "AskUserQuestion", "SendMessage", "Monitor", "Workflow", "SubagentHandback",
        "ListMcpResourcesTool", "ReadMcpResourceTool", "EnterWorktree", "ExitWorktree",
        "web_search", "web_fetch", "code_execution",
    }
)


def home_dir() -> Path:
    return Path(os.environ.get("TOKENCOUNT_HOME") or Path.home() / ".tokencount")


def load_salt() -> bytes:
    """The local secret behind record IDs; created on first use, never exported."""
    path = home_dir() / "salt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_hex(32)
    path.write_text(salt + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return salt.encode()


def record_id(task: Task, salt: bytes) -> str:
    return hmac.new(salt, task.key.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


class Labels:
    """Outcome, category and description per task, kept in ~/.tokencount/labels.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or home_dir() / "labels.json"
        try:
            self.data: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.data = {}

    def get(self, task: Task) -> dict:
        return self.data.get(task.key, {})

    def update(self, task: Task, **fields: str | None) -> dict:
        label = dict(self.data.get(task.key, {}))
        label.update({k: v for k, v in fields.items() if v is not None})
        self.data[task.key] = label
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return label


def tool_bucket(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    return name if name in SHAREABLE_TOOLS else "other"


def to_record(task: Task, salt: bytes, label: dict | None = None, include_prompt: bool = False) -> dict:
    """Build one schema-v0.1 record. Raises ValueError if the task has no date."""
    started = task.first_seen
    if started is None:
        raise ValueError("task has no timestamp")
    label = label or {}
    tools: Counter = Counter()
    for name, count in task.tools.items():
        tools[tool_bucket(name)] += count
    calls = sorted(task.calls, key=lambda c: c.timestamp or started)
    prompt = task.prompt_text if task.kind != "continuation" else None
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": record_id(task, salt),
        "harness": {"name": HARNESS, "version": task.harness_version},
        "importer": f"tokencount/{__version__}",
        "date": started.date().isoformat(),
        "primary_model": task.primary_model,
        "effort": task.effort,
        "turns": len(task.main_calls),
        "subagent_calls": len(task.subagent_calls),
        "duration_s": task.duration_s,
        "peak_context_tokens": task.peak_context_tokens,
        "tools": dict(sorted(tools.items())),
        "server_tools": {"web_search_requests": sum(c.web_search_requests for c in calls)},
        "calls": [
            {
                "role": c.role,
                "model": c.model,
                "effort": c.effort,
                "speed": c.speed,
                "inference_geo": c.inference_geo,
                "input_fresh": c.input_fresh,
                "cache_write_5m": c.cache_write_5m,
                "cache_write_1h": c.cache_write_1h,
                "cache_read": c.cache_read,
                "output": c.output,
                "thinking": c.thinking,
            }
            for c in calls
        ],
        "task": {
            "kind": task.kind,
            "category": label.get("category"),
            "description": label.get("description"),
            "prompt_chars": len(prompt) if prompt is not None else None,
            "prompt": prompt if include_prompt else None,
        },
        "outcome": {
            "label": label.get("outcome", "unknown"),
            "interrupted": task.interrupted,
        },
    }
