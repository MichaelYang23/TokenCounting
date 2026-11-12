# TokenCounting

**How many tokens, and dollars, does an AI agent task really take?**

A model's per-token price doesn't tell you what a task will cost. In *[The Price Reversal Phenomenon](https://arxiv.org/abs/2603.23971)* (Chen et al., 2026), the model with the lower list price ended up costing more in 32% of 336 model pairs, by up to 28×. The causes were extra thinking tokens on single questions and extra turns on agent tasks. Running the same query again on the same model varied in cost by up to 9.7×. The authors list predicting a query's *cost distribution* as an open problem.

Nobody can look up what a task like theirs usually costs on a given model, because that data isn't public. TokenCounting collects it:

1. **`tokencount`**, a zero-dependency CLI. It reads the logs your agent already writes and shows the cost per task, where the tokens and money went, and your most expensive tasks. Everything runs locally.
2. **An open record format** for sharing de-identified, per-task data points: token counts per call, model, effort, turns, tools, and whether the task worked.
3. **A dataset in this repository**, built from contributed records (see [data/README.md](data/README.md)).

Next: a one-command `tokencount submit`, a small validating API, and a website for browsing the data and looking up what tasks like yours cost on each model. Importers for more agents (Codex CLI, Gemini CLI, …) and a paired-run track, where the same task runs on several models, come after that. The plan is in [docs/roadmap.md](docs/roadmap.md).

## Quick start

Requires Python 3.9 or newer; no other dependencies. Supports Claude Code today.

```bash
git clone https://github.com/MichaelYang23/TokenCounting.git
```

```bash
cd TokenCounting && python3 -m tokencount report
```

To install it as a command instead, run `uv tool install git+https://github.com/MichaelYang23/TokenCounting` or the `pipx` equivalent.

Example output, from the synthetic logs the tests use (`tests/fixtures.py`):

```
TokenCounting · Claude Code
4 task(s) in 2 session(s) · 2026-11-01 → 2026-11-02 · prices as of 2026-11-12

Cost per task (USD)
                              tasks      p10      p50      p90      max  turns p50
  all tasks                       4  $0.0020  $0.0069   $0.016   $0.019          2
  claude-opus-5-5 · high          2   $0.011   $0.014   $0.018   $0.019          2
  claude-sonnet-4-5 · -           1  $0.0010  $0.0010  $0.0010  $0.0010          1
  claude-sonnet-5-5 · medium      1  $0.0041  $0.0041  $0.0041  $0.0041          1

Where the money went
                    tokens     cost  share
  input (uncached)     150  $0.0004     1%
  cache writes        7.5K   $0.017    51%
  cache reads        31.1K  $0.0055    16%
  output (visible)     495  $0.0067    20%
  thinking             215  $0.0042    12%
  total              39.5K   $0.034   100%

Most expensive tasks
  id        date        model              effort  turns  sub     cost  prompt
  b7ec7b70  2026-11-01  claude-opus-5-5    high        2    0   $0.019  Add a CSV export to the report page
  dfd031d8  2026-11-01  claude-opus-5-5    high        2    2  $0.0097  (interrupted) Why is the build failing?
  22053b7c  2026-11-01  claude-sonnet-5-5  medium      1    0  $0.0041  Summarize the README
  a7a61617  2026-11-02  claude-sonnet-4-5  -           1    0  $0.0010  Rename the export button
```

On real agent work the pattern is starker. The two tasks that started this repository were researching the idea and writing v0.1. Across them, cache reads were 94% of all tokens but only 22% of the cost, and thinking tokens that never appear on screen were 32% of the cost.

## Commands

| Command | What it does |
|---|---|
| `tokencount report` | Cost per task (p10/p50/p90/max) overall and by model × effort, where the tokens and money went, and your most expensive tasks. `--json` for machine-readable output, `--no-prompts` for screenshots. |
| `tokencount tasks` | Recent tasks with their ids, costs and labels. |
| `tokencount label <id> --outcome success --category feature --description "add CSV export to a Flask app"` | Record whether a task worked and what it was. Labels are stored locally in `~/.tokencount/labels.json`. |
| `tokencount export -o mine.jsonl` | Write de-identified records for sharing. `--only-labeled` keeps only the tasks you labeled. |
| `tokencount validate mine.jsonl` | Check records against the schema. |

By default the commands read `~/.claude/projects` and `~/.config/claude/projects`. Pass `--path` for another directory or a single session file, and `--since YYYY-MM-DD` to limit the date range.

A **task** is one prompt you typed plus every model call made until your next prompt, including calls by sub-agents spawned along the way.

## How costs are computed

- **Tokens** come from the `usage` block Claude Code logs for every API response: uncached input, 5-minute and 1-hour cache writes, cache reads, output, and the thinking tokens within output. Each response is counted once, even when it spans several log lines or reappears in a resumed session.
- **Prices** come from [`tokencount/prices.json`](tokencount/prices.json), Anthropic's list prices as of 2026-11-12. The table covers each model's own cache rates, Haiku 5.5's long-context rates, fast mode, and the 1.1× multiplier for US-only inference. Records store tokens, never dollars, so costs can be recomputed whenever prices change.
- If you are on a subscription plan, read the figures as the equivalent API cost.
- **Not included** are model calls that tools make on their own without logging them; in Claude Code, that means WebSearch and WebFetch. Batch discounts are not included either.

## Privacy

Everything runs on your machine. `export` writes a file, which you read before deciding to share it.

| A record contains | A record never contains |
|---|---|
| Per-call token counts, model, effort, speed | Prompt text (unless you pass `--with-prompt`), responses |
| Task date (no time of day), duration, turns, sub-agent calls | File paths, project names, session or request IDs |
| Tool-call counts: built-in names; MCP tools as `mcp`, others as `other` | MCP server or custom tool names |
| Your labels: outcome, category, a one-line description you wrote | Anything else from your logs |

Record IDs are HMACs keyed by a random salt in `~/.tokencount/salt`, which stays on your machine. IDs therefore stay the same when you re-export, but nobody can link them back to your logs.

The records don't include embeddings of your prompts either. Embeddings can be inverted to recover much of the original text, so they are no safer than the text itself.

## Contributing

- **Data:** see [data/README.md](data/README.md).
- **Code:** the tests use only the standard library. Run them with `python3 -m unittest discover -s tests`. Importers for other agents are especially welcome.

The record format is defined in [`tokencount/schemas/task_record.v0.1.json`](tokencount/schemas/task_record.v0.1.json), and [docs/design.md](docs/design.md) explains why it looks the way it does.

## License

- **Code:** [MIT](LICENSE).
- **Data in `data/records/`:** [CC0 1.0](data/LICENSE.md), dedicated to the public domain, so researchers and routing tools can reuse it freely.
