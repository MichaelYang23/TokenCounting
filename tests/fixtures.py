"""Builds a small, synthetic Claude Code projects directory for the tests.

Session 1 (2026-11-01):
  u1  "Add a CSV export..."      -> req_A (logged as two entries), tool result, req_B (old-style cache total)
  m1  meta entry                  -> must not start a task
  u2  "Why is the build failing?" -> req_C spawns sub-agent abc123 (req_E, req_F; req_F finishes
                                     after u3 started, so only the .meta.json link attributes it right),
                                     a synthetic API-error entry (ignored), req_D calls an MCP tool,
                                     then the user interrupts
  c1  /clear                      -> no model call, dropped
  u3  "Summarize the README"      -> req_G
Session 2 (2026-11-02), a resumed session that repeats u1 and req_A:
  u1  (copy)                      -> no new calls, dropped
  u4  "Rename the export button"  -> req_H on a dated Sonnet 4.5 ID, no effort or thinking fields
"""

from __future__ import annotations

import json
from pathlib import Path

S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"


def _usage(inp, cache_read, out, w1h=0, w5m=0, thinking=None, total_write=None, **extra):
    usage = {
        "input_tokens": inp,
        "cache_creation_input_tokens": w1h + w5m if total_write is None else total_write,
        "cache_read_input_tokens": cache_read,
        "output_tokens": out,
    }
    if total_write is None:
        usage["cache_creation"] = {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h}
    if thinking is not None:
        usage["output_tokens_details"] = {"thinking_tokens": thinking}
    usage.update(extra)
    return usage


def _user(uuid, sid, ts, content, **extra):
    entry = {"type": "user", "uuid": uuid, "sessionId": sid, "timestamp": ts, "isSidechain": False,
             "version": "2.1.300", "cwd": "/home/dev/secret-project", "message": {"role": "user", "content": content}}
    entry.update(extra)
    return entry


def _assistant(uuid, sid, ts, req, model, blocks, usage, effort="high", **extra):
    entry = {"type": "assistant", "uuid": uuid, "sessionId": sid, "timestamp": ts, "isSidechain": False,
             "requestId": req, "version": "2.1.300", "cwd": "/home/dev/secret-project",
             "message": {"id": "msg_" + req, "model": model, "role": "assistant", "content": blocks, "usage": usage}}
    if effort:
        entry["effort"] = effort
    entry.update(extra)
    return entry


def _tool_result(uuid, sid, ts, tool_id):
    return _user(uuid, sid, ts, [{"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}],
                 toolUseResult={"stdout": "ok"}, sourceToolAssistantUUID="x")


def _write(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
USAGE_A = _usage(10, 5000, 300, w1h=1000, thinking=200, speed="standard", inference_geo="global")


def build_projects(root: Path) -> Path:
    projects = root / "projects"
    project = projects / "-home-dev-secret-project"
    _write(project / f"{S1}.jsonl", [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-11-01T09:59:59.000Z"},
        _user("u1", S1, "2026-11-01T10:00:00.000Z", "Add a CSV export to the report page"),
        _assistant("a1", S1, "2026-11-01T10:00:05.000Z", "req_A", OPUS, [{"type": "thinking", "thinking": ""}], USAGE_A),
        _assistant("a2", S1, "2026-11-01T10:00:06.000Z", "req_A", OPUS,
                   [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}], USAGE_A),
        _tool_result("t1", S1, "2026-11-01T10:00:07.000Z", "toolu_1"),
        _assistant("a3", S1, "2026-11-01T10:00:20.000Z", "req_B", OPUS, [{"type": "text", "text": "Done."}],
                   _usage(5, 6000, 100, total_write=200)),
        _user("m1", S1, "2026-11-01T10:01:00.000Z", "Caveat: local command output follows.", isMeta=True),
        _user("u2", S1, "2026-11-01T10:05:00.000Z", [{"type": "text", "text": "Why is the build failing?"}]),
        _assistant("a4", S1, "2026-11-01T10:05:03.000Z", "req_C", OPUS,
                   [{"type": "tool_use", "id": "toolu_2", "name": "Agent", "input": {"description": "CI logs"}}],
                   _usage(4, 8000, 50, thinking=10)),
        _assistant("a5", S1, "2026-11-01T10:05:04.000Z", "req_err", "<synthetic>",
                   [{"type": "text", "text": "API Error"}], _usage(0, 0, 0), isApiErrorMessage=True),
        _tool_result("t2", S1, "2026-11-01T10:05:20.000Z", "toolu_2"),
        _assistant("a6", S1, "2026-11-01T10:05:30.000Z", "req_D", OPUS,
                   [{"type": "tool_use", "id": "toolu_3", "name": "mcp__acme_internal__lookup", "input": {}}],
                   _usage(6, 8100, 40, w1h=500, thinking=0)),
        _user("i1", S1, "2026-11-01T10:05:40.000Z", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]),
        _user("c1", S1, "2026-11-01T10:06:00.000Z", "<command-name>/clear</command-name>\n<command-args></command-args>"),
        _user("l1", S1, "2026-11-01T10:06:01.000Z", "<local-command-stdout></local-command-stdout>"),
        _user("u3", S1, "2026-11-01T10:10:00.000Z", "Summarize the README"),
        _assistant("a7", S1, "2026-11-01T10:10:05.000Z", "req_G", "claude-sonnet-5-5",
                   [{"type": "text", "text": "Summary."}], _usage(20, 0, 30, w5m=1500, thinking=5), effort="medium"),
    ])

    agent_dir = project / S1 / "subagents"
    _write(agent_dir / "agent-abc123.jsonl", [
        dict(_user("s0", S1, "2026-11-01T10:05:04.000Z", "Inspect the CI logs"), isSidechain=True, agentId="abc123"),
        dict(_assistant("s1", S1, "2026-11-01T10:05:08.000Z", "req_E", HAIKU,
                        [{"type": "tool_use", "id": "toolu_9", "name": "Read", "input": {"file_path": "/x"}}],
                        _usage(3, 0, 80, w5m=4000, thinking=0), effort="low"), isSidechain=True, agentId="abc123"),
        dict(_tool_result("s2", S1, "2026-11-01T10:05:09.000Z", "toolu_9"), isSidechain=True, agentId="abc123"),
        dict(_assistant("s3", S1, "2026-11-01T10:10:30.000Z", "req_F", HAIKU, [{"type": "text", "text": "Found it."}],
                        _usage(2, 4000, 60, w5m=300, thinking=0), effort="low"), isSidechain=True, agentId="abc123"),
    ])
    (agent_dir / "agent-abc123.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "toolUseId": "toolu_2"}), encoding="utf-8")

    _write(project / f"{S2}.jsonl", [
        _user("u1", S2, "2026-11-01T10:00:00.000Z", "Add a CSV export to the report page"),
        _assistant("a1", S2, "2026-11-01T10:00:05.000Z", "req_A", OPUS, [{"type": "thinking", "thinking": ""}], USAGE_A),
        _user("u4", S2, "2026-11-02T09:00:00.000Z", "Rename the export button"),
        _assistant("a8", S2, "2026-11-02T09:00:04.000Z", "req_H", "claude-sonnet-4-5-20250929",
                   [{"type": "text", "text": "Renamed."}], _usage(100, 0, 50), effort=None),
    ])
    return projects
