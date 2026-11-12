# Design notes

This file explains the choices behind the record format and the tool, and the gaps we already know about.

## The question

Before running a task, people want to know what a task like theirs costs on model X, and how much that cost varies. List prices can't answer this. *[The Price Reversal Phenomenon](https://arxiv.org/abs/2603.23971)* found:

- On single-turn tasks, the cheaper-listed model cost more mainly because it thought longer. More than 95% of the cost gap in reversed pairs came from thinking tokens.
- On agent tasks, it cost more because it took more turns, and each turn re-reads a growing context.

Routers such as FrugalGPT, and products like OpenRouter's Auto Router, need a per-query cost estimate. No public data to build one from exists yet.

## A record is a task, not an API call

Price reversal shows up at the level of a whole task: number of turns × context growth × thinking per turn. One record therefore covers one human prompt and every model call it caused, sub-agents included.

Each record keeps every call's token counts in order. Three things depend on per-call data:

- **Pricing** that depends on the size of each request, such as Haiku 5.5's long-context rate.
- **Context-growth curves**, one point per turn.
- **The paper's cost decomposition**: turns, fresh input, output, cache and thinking tokens per turn, plus the three prices.

## Store tokens, derive dollars

Prices change, and cache discounts differ by model and generation. For example, Opus 5.5's cache reads cost 5% of its input price, while Opus 4.x's cost 10%. Records therefore hold token counts by category:

- uncached input
- 5-minute cache writes
- 1-hour cache writes
- cache reads
- output
- thinking (a subset of output)

Cost is computed from a dated price table ([`tokencount/prices.json`](../tokencount/prices.json)). Token counts by category matter because they don't track cost: in agent sessions, cache reads are often more than 90% of tokens but a fraction of the bill.

## Record whether it worked

A cheap run that failed is not cheap. The number that matters is cost per *successful* task, including retries, so records carry an outcome label (`success`, `partial`, `fail`, `unknown`) and an `interrupted` flag. Only the contributor knows the outcome, which is why `tokencount label` exists.

## Observational data has a selection problem

People tend to give hard tasks to expensive models and easy tasks to cheap ones. A naive average cost per model therefore makes expensive models look worse, which hides exactly the reversal this project wants to measure.

Matching on prompt similarity only partly controls for difficulty, because difficulty often lives outside the prompt. "Fix this bug" can cost very different amounts in two different repositories. Two things help:

- **Rich task features**: category, description, prompt length, peak context, tools used.
- **Paired runs**: the same task run on several models, several times each. This is planned as a separate track, starting with tasks whose prompts contributors choose to publish.

## Distributions, not point estimates

The same query on the same model can vary in cost by nearly 10×. Any lookup should therefore show percentiles (p10/p50/p90) for similar tasks rather than a single number. The report already does this per model and effort.

## Token counts don't compare across models

Tokenizers differ between vendors, and sometimes between generations from the same vendor. Anthropic's tokenizer from Claude 4.7 onward produces about 30% more tokens for the same text. Compare models on dollars, or on tokens within one tokenizer family.

## Privacy

- **No prompt text by default.** Prompts often contain code, company data or personal details.
- **No embeddings either.** Text embeddings can be inverted to recover much of the original text, so they don't make prompts safe to share.
- **Instead**, records hold structured features plus an optional one-line description the contributor writes themselves. Prompt text is opt-in (`--with-prompt`).
- **Similarity search on the planned website** will run in the browser: the page downloads the public index and embeds your prompt locally, so the prompt never leaves your machine.
- **Record IDs** are HMACs keyed by a local salt. They stay stable when you re-export, but can't be linked back to your logs.
- **Tool names** are reduced to built-in names, `mcp` and `other`, so private MCP server names don't leak.

## Data lives in git

Contributions are pull requests that add JSONL files under `data/records/`, and CI validates them against the schema. This keeps the dataset transparent, versioned and forkable with no server to run. If it outgrows git, the same files can be loaded into a database.

## Known gaps in the Claude Code importer

- **Tools that call models on their own aren't counted.** Some tools, such as WebSearch and WebFetch, make model calls that are not written to the session log.
- **Older logs report no thinking-token count.** For these, `thinking` is `null`, meaning unknown rather than zero.
- **Older logs give only a total for cache writes.** It is counted as 5-minute writes, the API default.
- **Some sub-agents are attributed by time.** Sub-agents in older logs have no link to the tool call that spawned them, so they are attributed to whichever task was running at the time. Newer logs link them through `agent-*.meta.json`.
- **Messages typed while the agent is working** join the task that is already running.
- **`duration_s` is wall-clock time.** It includes time spent waiting on permission prompts.
