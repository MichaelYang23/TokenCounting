"""Read Claude Code session logs and turn them into tasks.

Claude Code writes one JSONL file per session under ``~/.claude/projects/``
(or ``~/.config/claude/projects/``). Every model response is logged together
with the API's ``usage`` block, so the token counts here are what was billed.

A *task* is one human prompt plus every model call made until the next human
prompt, including the calls of any sub-agents spawned during that turn.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

HARNESS = "claude-code"

_INTERRUPT_PREFIX = "[Request interrupted by user"
_META_PREFIXES = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)
_COMMAND_NAME = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_COMMAND_ARGS = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class ApiCall:
    """One billed model response."""

    request_id: str
    model: str
    role: str  # "main" or "subagent"
    timestamp: datetime | None = None
    effort: str | None = None
    speed: str | None = None
    inference_geo: str | None = None
    input_fresh: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output: int = 0
    thinking: int | None = None  # None: the log does not say
    web_search_requests: int = 0

    @property
    def context_tokens(self) -> int:
        """Every input token the model read for this call, cached or not."""
        return self.input_fresh + self.cache_write_5m + self.cache_write_1h + self.cache_read


@dataclass
class Task:
    """One human prompt and every model call it caused."""

    session_id: str
    prompt_uuid: str
    prompt_text: str | None
    started_at: datetime | None
    kind: str = "prompt"  # "prompt", "command" or "continuation"
    harness_version: str | None = None
    calls: list[ApiCall] = field(default_factory=list)
    tools: Counter = field(default_factory=Counter)
    interrupted: bool = False
    ended_at: datetime | None = None

    @property
    def key(self) -> str:
        return f"{self.session_id}:{self.prompt_uuid}"

    @property
    def main_calls(self) -> list[ApiCall]:
        return [c for c in self.calls if c.role == "main"]

    @property
    def subagent_calls(self) -> list[ApiCall]:
        return [c for c in self.calls if c.role == "subagent"]

    @property
    def primary_model(self) -> str | None:
        calls = self.main_calls or self.calls
        if not calls:
            return None
        return Counter(c.model for c in calls).most_common(1)[0][0]

    @property
    def effort(self) -> str | None:
        efforts = [c.effort for c in self.main_calls if c.effort]
        return Counter(efforts).most_common(1)[0][0] if efforts else None

    @property
    def first_seen(self) -> datetime | None:
        stamps = [c.timestamp for c in self.calls if c.timestamp]
        return self.started_at or (min(stamps) if stamps else None)

    @property
    def duration_s(self) -> int | None:
        if self.started_at and self.ended_at:
            return max(0, int((self.ended_at - self.started_at).total_seconds()))
        return None

    @property
    def peak_context_tokens(self) -> int:
        return max((c.context_tokens for c in self.main_calls), default=0)


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def iter_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue  # e.g. a line still being written
            if isinstance(obj, dict):
                yield obj


def default_log_dirs() -> list[Path]:
    """Where Claude Code keeps session logs on this machine."""
    candidates = []
    for part in os.environ.get("CLAUDE_CONFIG_DIR", "").split(","):
        if part.strip():
            candidates.append(Path(part.strip()).expanduser() / "projects")
    home = Path.home()
    candidates += [home / ".config" / "claude" / "projects", home / ".claude" / "projects"]
    dirs, seen = [], set()
    for d in candidates:
        if d.is_dir() and d.resolve() not in seen:
            seen.add(d.resolve())
            dirs.append(d)
    return dirs


def find_log_files(paths: Iterable[Path]) -> list[Path]:
    """Every session and sub-agent log under ``paths``, in a stable order."""
    files: set[Path] = set()
    for p in paths:
        p = Path(p).expanduser()
        if p.is_file() and p.suffix == ".jsonl":
            files.add(p.resolve())
            subagents = p.with_suffix("") / "subagents"
            if subagents.is_dir():
                files.update(f.resolve() for f in subagents.glob("*.jsonl"))
        elif p.is_dir():
            files.update(f.resolve() for f in p.rglob("*.jsonl"))
    return sorted(files)


def classify_user_entry(entry: dict) -> str:
    """Label a main-thread ``user`` entry.

    Returns "prompt", "command", "interrupt", "tool_result" or "meta". Only
    prompts and commands start a new task.
    """
    if entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
        return "meta"
    if "toolUseResult" in entry or "sourceToolAssistantUUID" in entry:
        return "tool_result"
    content = (entry.get("message") or {}).get("content")
    text, has_media = "", False
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                return "tool_result"
            if kind == "text":
                parts.append(block.get("text") or "")
            elif kind in ("image", "document"):
                has_media = True
        text = "\n".join(parts)
    stripped = text.strip()
    if stripped.startswith(_INTERRUPT_PREFIX):
        return "interrupt"
    if stripped.startswith(_META_PREFIXES):
        return "meta"
    if "<command-name>" in stripped[:300]:
        return "command"
    if not stripped and not has_media:
        return "meta"
    return "prompt"


def _prompt_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    else:
        parts = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif block.get("type") in ("image", "document"):
                parts.append(f"[{block['type']}]")
        text = "\n".join(parts)
    name = _COMMAND_NAME.search(text)
    if name:
        args = _COMMAND_ARGS.search(text)
        return f"{name.group(1)} {args.group(1) if args else ''}".strip()
    return text


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_call(entry: dict, role: str) -> ApiCall | None:
    """The billed usage of one ``assistant`` entry, or None if it has none."""
    if entry.get("isApiErrorMessage"):
        return None
    message = entry.get("message") or {}
    usage = message.get("usage")
    model = message.get("model")
    request_id = entry.get("requestId") or message.get("id")
    if not isinstance(usage, dict) or not model or model == "<synthetic>" or not request_id:
        return None
    cache = usage.get("cache_creation") or {}
    write_5m = _int(cache.get("ephemeral_5m_input_tokens"))
    write_1h = _int(cache.get("ephemeral_1h_input_tokens"))
    write_total = _int(usage.get("cache_creation_input_tokens"))
    if write_5m + write_1h < write_total:
        # Older logs only give a total; 5 minutes is the API's default cache TTL.
        write_5m = write_total - write_1h
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    server = usage.get("server_tool_use") or {}
    return ApiCall(
        request_id=str(request_id),
        model=str(model),
        role=role,
        timestamp=parse_timestamp(entry.get("timestamp")),
        effort=_str(entry.get("effort")),
        speed=_str(usage.get("speed")),
        inference_geo=_str(usage.get("inference_geo")),
        input_fresh=_int(usage.get("input_tokens")),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
        cache_read=_int(usage.get("cache_read_input_tokens")),
        output=_int(usage.get("output_tokens")),
        thinking=None if thinking is None else _int(thinking),
        web_search_requests=_int(server.get("web_search_requests")),
    )


def _tool_uses(entry: dict) -> Iterator[tuple[str | None, str]]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use"):
                yield block.get("id"), str(block.get("name") or "unknown")


class _Session:
    def __init__(self, session_id: str, order: int):
        self.id = session_id
        self.order = order
        self.main: list[dict] = []
        self.side: dict[str, list[dict]] = {}
        self.agent_meta: dict[str, dict] = {}
        self.first: datetime | None = None

    def add(self, entry: dict, path: Path, meta: dict | None) -> None:
        stamp = parse_timestamp(entry.get("timestamp"))
        if stamp and (self.first is None or stamp < self.first):
            self.first = stamp
        if entry.get("isSidechain"):
            agent = str(entry.get("agentId") or path)
            self.side.setdefault(agent, []).append(entry)
            if meta and agent not in self.agent_meta:
                self.agent_meta[agent] = meta
        else:
            self.main.append(entry)


class _Seen:
    """Dedupes across files: resumed sessions can repeat earlier entries."""

    def __init__(self) -> None:
        self.requests: set[str] = set()
        self.tool_uses: set[str] = set()


def _read_agent_meta(path: Path) -> dict | None:
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


def _absorb(task: Task, entry: dict, role: str, seen: _Seen, tool_owner: dict[str, Task]) -> None:
    stamp = parse_timestamp(entry.get("timestamp"))
    if stamp and (task.ended_at is None or stamp > task.ended_at):
        task.ended_at = stamp
    for tool_id, name in _tool_uses(entry):
        if tool_id:
            if tool_id in seen.tool_uses:
                continue
            seen.tool_uses.add(tool_id)
            tool_owner[tool_id] = task
        task.tools[name] += 1
    call = parse_call(entry, role)
    if call and call.request_id not in seen.requests:
        seen.requests.add(call.request_id)
        task.calls.append(call)


def _task_at(tasks: list[Task], stamp: datetime | None) -> Task | None:
    """The task that was running at ``stamp``."""
    if not tasks:
        return None
    owner = tasks[0]
    if stamp is None:
        return owner
    for task in tasks:
        if task.started_at and task.started_at <= stamp:
            owner = task
    return owner


def _build_tasks(session: _Session, seen: _Seen) -> list[Task]:
    tasks: list[Task] = []
    tool_owner: dict[str, Task] = {}
    seen_uuids: set[str] = set()
    current: Task | None = None

    for entry in session.main:
        uuid = entry.get("uuid")
        if uuid:
            if uuid in seen_uuids:
                continue
            seen_uuids.add(uuid)
        if entry.get("type") == "user":
            kind = classify_user_entry(entry)
            if kind in ("prompt", "command"):
                current = Task(
                    session_id=session.id,
                    prompt_uuid=str(uuid or f"prompt-{len(tasks)}"),
                    prompt_text=_prompt_text(entry),
                    started_at=parse_timestamp(entry.get("timestamp")),
                    kind=kind,
                    harness_version=_str(entry.get("version")),
                )
                tasks.append(current)
            elif kind == "interrupt" and current is not None:
                current.interrupted = True
            continue
        if current is None:  # the log starts mid-conversation
            current = Task(
                session_id=session.id,
                prompt_uuid=str(uuid or "continuation"),
                prompt_text=None,
                started_at=parse_timestamp(entry.get("timestamp")),
                kind="continuation",
                harness_version=_str(entry.get("version")),
            )
            tasks.append(current)
        _absorb(current, entry, "main", seen, tool_owner)

    if session.side and not tasks:
        tasks.append(Task(session.id, "continuation", None, session.first, kind="continuation"))

    # Sub-agents: their .meta.json names the tool call that spawned them. Resolve
    # repeatedly so that agents spawned by other agents land on the same task.
    pending = dict(session.side)
    progress = True
    while pending and progress:
        progress = False
        for agent, entries in list(pending.items()):
            tool_id = session.agent_meta.get(agent, {}).get("toolUseId")
            owner = tool_owner.get(tool_id) if tool_id else None
            if owner is None:
                continue
            for entry in entries:
                if entry.get("type") == "assistant":
                    _absorb(owner, entry, "subagent", seen, tool_owner)
            del pending[agent]
            progress = True
    # Older logs carry no link back; fall back to whichever task was running.
    for entries in pending.values():
        for entry in entries:
            if entry.get("type") == "assistant":
                owner = _task_at(tasks, parse_timestamp(entry.get("timestamp")))
                if owner is not None:
                    _absorb(owner, entry, "subagent", seen, tool_owner)
    return tasks


def load_tasks(paths: Iterable[Path]) -> list[Task]:
    """Parse every Claude Code log under ``paths`` into tasks, oldest first.

    Tasks that made no billed model call (e.g. ``/clear``) are dropped.
    """
    sessions: dict[str, _Session] = {}
    for path in find_log_files(paths):
        meta = _read_agent_meta(path)
        for entry in iter_jsonl(path):
            if entry.get("type") not in ("user", "assistant"):
                continue
            sid = str(entry.get("sessionId") or path.stem)
            if sid not in sessions:
                sessions[sid] = _Session(sid, len(sessions))
            sessions[sid].add(entry, path, meta)

    seen = _Seen()
    tasks: list[Task] = []
    for session in sorted(sessions.values(), key=lambda s: (s.first or _EPOCH, s.order)):
        tasks.extend(_build_tasks(session, seen))
    tasks = [t for t in tasks if t.calls]
    tasks.sort(key=lambda t: t.first_seen or _EPOCH)
    return tasks
