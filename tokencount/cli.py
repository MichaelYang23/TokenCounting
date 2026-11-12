"""Command line: tokencount report | tasks | label | export | validate."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from . import __version__
from .claude_code import Task, default_log_dirs, load_tasks
from .pricing import PriceTable
from .records import CATEGORIES, OUTCOMES, Labels, load_salt, record_id, to_record
from .report import preview, render_report, render_tasks, summarize
from .schema import load_schema, validate_record


def _add_source_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--path",
        action="append",
        type=Path,
        help="a Claude Code projects directory or a session .jsonl file; repeatable "
        "(default: ~/.claude/projects and ~/.config/claude/projects)",
    )
    p.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="only tasks started on or after this date")


def _load(args: argparse.Namespace) -> list[Task]:
    paths = args.path or default_log_dirs()
    if not paths:
        raise SystemExit("No Claude Code logs found. Point --path at a projects directory or a session .jsonl file.")
    tasks = load_tasks(paths)
    if args.since:
        tasks = [t for t in tasks if t.first_seen and t.first_seen.date() >= args.since]
    return tasks


def _short_ids(tasks: Sequence[Task]) -> dict[str, str]:
    salt = load_salt()
    return {t.key: record_id(t, salt)[:8] for t in tasks}


def cmd_report(args: argparse.Namespace) -> int:
    tasks = _load(args)
    if not tasks:
        print("No tasks with billed model calls found.")
        return 0
    ids = _short_ids(tasks)
    summary = summarize(tasks, PriceTable.load(), short_id=lambda t: ids[t.key], top=args.top)
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    previews = None if args.no_prompts else {ids[t.key]: preview(t.prompt_text) for t in tasks}
    print(render_report(summary, previews))
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    tasks = _load(args)
    ids = _short_ids(tasks)
    labels = Labels()
    recent = list(reversed(tasks))[: args.limit]
    print(render_tasks(recent, PriceTable.load(), lambda t: ids[t.key], labels.get, show_prompts=not args.no_prompts))
    return 0


def cmd_label(args: argparse.Namespace) -> int:
    tasks = _load(args)
    ids = _short_ids(tasks)
    matches = [t for t in tasks if ids[t.key].startswith(args.id.lower())]
    if not matches:
        print(f"No task id starts with {args.id!r}. List them with: tokencount tasks", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{args.id!r} matches {len(matches)} tasks; use more characters.", file=sys.stderr)
        return 1
    task = matches[0]
    label = Labels().update(task, outcome=args.outcome, category=args.category, description=args.description)
    print(f"{ids[task.key]}: {json.dumps(label, ensure_ascii=False)}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    tasks = _load(args)
    salt = load_salt()
    labels = Labels()
    schema = load_schema()
    records = []
    for task in tasks:
        label = labels.get(task)
        if args.only_labeled and not label:
            continue
        try:
            record = to_record(task, salt, label, include_prompt=args.with_prompt)
        except ValueError:
            continue
        errors = validate_record(record, schema)
        if errors:
            raise SystemExit(f"Internal error, record does not match the schema: {errors[:3]}")
        records.append(record)
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(lines, encoding="utf-8")
        where = str(args.output)
    else:
        sys.stdout.write(lines)
        where = "stdout"
    note = "WITH prompt text" if args.with_prompt else "without prompt text, file paths or session IDs"
    print(f"Wrote {len(records)} record(s) to {where}, {note}. Read it before you share it.", file=sys.stderr)
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    schema = load_schema()
    bad = 0
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    errors = validate_record(json.loads(line), schema)
                except json.JSONDecodeError as exc:
                    errors = [f"not valid JSON ({exc.msg})"]
                for error in errors:
                    print(f"{path}:{n}: {error}")
                bad += bool(errors)
    print(f"{'OK' if not bad else f'{bad} invalid record(s)'}", file=sys.stderr)
    return 1 if bad else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokencount",
        description="How many tokens (and dollars) do your agent tasks really take? Reads Claude Code logs locally.",
    )
    parser.add_argument("--version", action="version", version=f"tokencount {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="cost per task, where the tokens went, most expensive tasks")
    _add_source_args(p)
    p.add_argument("--top", type=int, default=5, help="how many expensive tasks to list (default 5)")
    p.add_argument("--no-prompts", action="store_true", help="hide prompt previews (for screenshots)")
    p.add_argument("--json", action="store_true", help="machine-readable output, never includes prompt text")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("tasks", help="list recent tasks with their ids")
    _add_source_args(p)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--no-prompts", action="store_true", help="hide prompt previews")
    p.set_defaults(func=cmd_tasks)

    p = sub.add_parser("label", help="record whether a task succeeded, its category and a one-line description")
    _add_source_args(p)
    p.add_argument("id", help="task id (or a unique prefix) from `tokencount tasks`")
    p.add_argument("--outcome", choices=OUTCOMES)
    p.add_argument("--category", choices=CATEGORIES)
    p.add_argument("--description", help="one line you are happy to publish, e.g. 'add CSV export to a Flask app'")
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("export", help="write de-identified task records (JSONL) for sharing")
    _add_source_args(p)
    p.add_argument("-o", "--output", type=Path, help="output file (default: stdout)")
    p.add_argument("--only-labeled", action="store_true", help="only tasks you have labeled")
    p.add_argument("--with-prompt", action="store_true", help="include full prompt text (off by default)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("validate", help="check JSONL record files against the schema")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_validate)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "label":
        if not (args.outcome or args.category or args.description):
            print("Nothing to set: pass --outcome, --category and/or --description.", file=sys.stderr)
            return 2
        if args.description and len(args.description) > 300:
            print("Keep --description to one line of at most 300 characters.", file=sys.stderr)
            return 2
    return args.func(args)
