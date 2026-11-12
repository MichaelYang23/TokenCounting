# Roadmap: shared dataset and website

## Positioning

Three kinds of tools already exist, and each covers only part of the question:

- **Usage leaderboards** (Viberank, ccclub and others) answer "who uses how much". They collect daily totals per person.
- **Cost benchmarks** (HAL, Artificial Analysis, the price-reversal paper) measure cost per task, but only on fixed test sets.
- **Pre-call estimators** such as Calcis predict how long a single call's output will be.

TokenCounting answers a different question: **what does a task like mine cost on each model, and how often does it work?** No public source we found combines all four of the following:

1. per-task records,
2. from real work, contributed by many people,
3. with outcomes,
4. searchable by kind of task.

## Lessons from existing tools

- **People share usage when it takes one command.** Viberank had about 800 contributors by June 2026 and shows about 1,400 now. Its design:
  - submission is one CLI command, with optional daily auto-submit;
  - the server validates every upload (token sums, per-model cost floors, date checks);
  - uploads made after GitHub sign-in are marked verified.

  Our pull-request flow asks far more of contributors, so v0.2 replaces it with a one-command submit.
- **ccusage can't be reused as code.** It reads logs from 19 coding agents (MIT licensed), but since v20 it ships as native binaries with no library API, and its JSON output stops at daily, weekly, monthly and session totals, which cannot be split into tasks. So we:
  - write our own per-task importers, using ccusage's source as a reference for where each agent keeps its logs and how it records usage;
  - cross-check our daily totals against `ccusage daily --json` in tests;
  - propose a per-entry JSON export upstream. If it lands, one importer could cover every agent ccusage supports.

## Architecture

```
your machine                     server (Cloudflare)                     public (CC0)
tokencount CLI --- submit ---->  API Worker --- validate ---> D1 ---->  nightly snapshot
  reads logs, labels,            schema, consistency,                     dataset files (GitHub release,
  previews what is sent          plausibility, description                Hugging Face mirror)
  (records only)                 screening, rate limits, dedup            summary.json --> static website
```

The website reads published records only and never sees raw logs. That leaves one log parser to maintain: the Python CLI. Personal reports stay local, in the CLI.

## Components

### CLI (Python, published to PyPI as `tokencount`)

- **Run without cloning:** `uvx tokencount report` or `pipx run tokencount report`. The package name is still free on PyPI.
- **`tokencount submit`:** shows exactly which records will be sent, asks for confirmation, then uploads them. Resubmitting the same task updates its labels instead of adding a duplicate.
- **`tokencount login`** (optional): uses GitHub's device flow, and submissions made afterwards are marked verified. Without it, submissions are accepted as unverified with a lower rate limit.
- **Description check before upload:** the CLI warns about e-mail addresses, URLs, file paths and key-like strings.
- **More importers:** Codex CLI and Gemini CLI first, then others as people ask for them.
- **Later:** opt-in daily auto-submit.

### API (Cloudflare Worker + D1)

`POST /v1/records` accepts a batch of records and runs these checks:

1. **Schema v0.1.**
2. **Consistency:**
   - `turns` and `subagent_calls` match the calls;
   - thinking ≤ output;
   - `peak_context_tokens` matches the calls.
3. **Plausibility:**
   - known model names;
   - token counts within each model's limits;
   - dates not in the future;
   - cost under a ceiling.
4. **Description screening:** the same patterns the CLI warns about.
5. **Deduplication** by contributor and `record_id`.

Identity: the server verifies the GitHub token by calling the GitHub API once, then keeps only a keyed hash of the GitHub user ID. No usernames or tokens are stored.

`GET /v1/summary` returns the aggregates the website shows.

Moderation: statistical outliers are flagged and kept out of published statistics until reviewed.

### Published dataset

- A nightly snapshot of accepted records, as JSONL and Parquet, under CC0 1.0.
- Published as GitHub release assets and mirrored to a Hugging Face dataset for researchers.
- Each record carries a `verified` flag and a pseudonymous contributor ID.

### Website (static, Cloudflare Pages)

- **Browse:**
  - cost per task (p10/p50/p90) by model, effort and category;
  - success rate, turns and thinking share;
  - a "same kind of task, different models" comparison;
  - filters for date range, agent and verified-only.
- **Look up**, once a few hundred labeled records exist:
  - paste a prompt; its embedding is computed in the browser and is never sent anywhere;
  - shows similar tasks and each model's cost distribution and success rate among them;
  - always shows how many tasks the estimate rests on.
- **Contribute:** the one-command instructions, exactly what is and isn't shared, and a privacy FAQ.
- **Methodology:** the price-table version and the known gaps (see [design.md](design.md)).

## Phases

| Version | Scope | Outcome |
|---|---|---|
| v0.2 | PyPI release; `submit` and `login`; API with validation; nightly snapshot | Contributions take one command |
| v0.3 | Website Browse, Contribute and Methodology pages; dataset download; Codex CLI and Gemini CLI importers | Anyone can see the data; more agents covered |
| v0.4 | Look up; paired runs (the same task on several models); outcome hints from interruptions and retries to reduce labeling work | The "what will this cost" question gets an answer |

## Needs the project owner

- **A Cloudflare account** for Workers, D1 and Pages. The free tier should cover early traffic.
- **A GitHub OAuth App** with device flow enabled, for verified submissions. The CLI needs only its client ID, not a secret.
- **A PyPI account** to publish `tokencount`.
- **Optional:** a custom domain, and a Hugging Face dataset repository for the mirror.

## Risks

- **Junk or fake data.** Mitigated by consistency and plausibility checks, the verified flag, outlier review, and a verified-only filter on the website.
- **Privacy at task granularity.** Mitigated by screening descriptions, sending no prompt text unless the contributor opts in, and publishing pseudonymous contributor IDs.
- **Cold start.** The local report is useful without anyone else's data. Our own records seed the dataset, and the price-reversal paper's data can be added if its authors release it.
- **Price drift.** Records store tokens and `prices.json` is versioned, so costs are always recomputed rather than stored.
