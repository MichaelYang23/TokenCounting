# Contributing data

Each line of a file in `data/records/` is one task in the [v0.1 record format](../tokencount/schemas/task_record.v0.1.json). Contributions are pull requests that add a file.

## Steps

1. **Find tasks worth sharing.**

   ```bash
   python3 -m tokencount tasks --limit 50
   ```

2. **Label them.** The label records whether each task worked and what kind of task it was. The description is published, so write one line with no names, secrets or internal details.

   ```bash
   python3 -m tokencount label 3f2a9c1b --outcome success --category feature --description "build a CLI that parses agent logs into per-task token reports"
   ```

3. **Export.**

   ```bash
   python3 -m tokencount export --only-labeled -o data/records/<your-handle>-<YYYY-MM-DD>.jsonl
   ```

4. **Read the file before you share it.** Each line should contain only token counts, model and effort names, dates, tool counts and your labels. To keep a task private, delete its line.

5. **Validate**, then open a pull request.

   ```bash
   python3 -m tokencount validate data/records/<your-file>.jsonl
   ```

## Guidelines

- Labeled records are the most useful. Knowing whether a cheap run actually solved the task is what separates a price reversal from a false economy.
- Unlabeled records are still welcome, because they show how cost spreads by model and effort.
- Include failures and interrupted tasks. Leaving them out would make every model look cheaper than it is.
- Never submit someone else's logs.
- Records are published under [CC0 1.0](LICENSE.md), which puts them in the public domain. Opening a pull request means you agree to that.
