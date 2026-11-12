"""Download every published record from the API and write a dataset snapshot.

Used by .github/workflows/dataset-snapshot.yml, but runs anywhere with Python 3.9+:

    python3 scripts/snapshot.py --api https://tokencount-api.example.workers.dev --out dist
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tokencount import remote  # noqa: E402
from tokencount.schema import load_schema, validate_record  # noqa: E402


def fetch(api: str, path: str) -> dict:
    status, body = remote.request("GET", f"{api}{path}")
    if status != 200:
        raise SystemExit(f"GET {path} failed ({status}): {body.get('error', body)}")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True, help="API base URL")
    parser.add_argument("--out", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")

    schema = load_schema()
    rows, after = [], 0
    while True:
        page = fetch(api, f"/v1/export?limit=1000&after={after}")
        for row in page["records"]:
            errors = validate_record(row["record"], schema)
            if errors:
                raise SystemExit(f"A published record does not match the schema: {errors[:3]}")
            rows.append(row)
        if page["next"] is None:
            break
        after = page["next"]
    summary = fetch(api, "/v1/summary")

    day = datetime.now(timezone.utc).date().isoformat()
    args.out.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out / "tokencount-records.jsonl.gz", "wt", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    (args.out / "NOTES.md").write_text(
        f"Snapshot of the TokenCounting shared dataset, {day}.\n\n"
        f"- {len(rows)} published task records from {summary['contributors']} contributors "
        f"({summary['verified_records']} records GitHub-verified).\n"
        "- Each line of `tokencount-records.jsonl.gz` is `{\"contributor\", \"verified\", \"received\", \"record\"}`; "
        "`record` follows [schema v0.1](https://github.com/MichaelYang23/TokenCounting/blob/main/tokencount/schemas/task_record.v0.1.json).\n"
        "- `summary.json` holds the aggregates the website shows.\n"
        "- Dedicated to the public domain under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/).\n",
        encoding="utf-8",
    )
    print(f"{len(rows)} records -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
