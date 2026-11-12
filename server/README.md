# TokenCounting API

A Cloudflare Worker with a D1 (SQLite) database. It receives records from `tokencount submit`, validates them, and serves the published dataset.

The schema, price table and sharing policy are not copied here. The Worker bundles them from `../tokencount/` (`schemas/task_record.v0.1.json`, `prices.json` and `policy.json`), so the CLI and the API always apply the same rules.

## Endpoints

| Method and path | What it does |
|---|---|
| `POST /v1/records` | Submit `{"records": [...]}`, at most 100 per request. Identity comes from `X-Contributor-Key` (anonymous, 32–128 hex characters), `Authorization: Bearer <GitHub token>` (verified), or both. With both, records sent earlier with that key move to the verified identity instead of being duplicated. The response has one result per record: `published`, `flagged` (stored, held for review) or `rejected`, with `new` and `reasons`. |
| `GET /v1/summary` | Aggregates over published records: counts, cost per task (p10/p50/p90/max) by model and effort, outcomes, categories. `?verified=1` restricts it to GitHub-verified records. |
| `GET /v1/export?after=<cursor>&limit=<n≤1000>` | Published records under CC0 1.0, each as `{contributor, verified, received, record}`. `next` is the cursor for the following page, or `null` on the last page. |
| `GET /v1/health` | The versions of the schema, price table and policy in use. |
| `GET /v1/admin/flagged` | Records held for review. Admin only. |
| `POST /v1/admin/review` | `{"contributor", "record_id", "action": "publish" \| "remove"}`. A removed record stays removed even if it is submitted again. Admin only. |
| `POST /v1/admin/recheck` | Re-evaluates held records against the deployed tables, for example after adding a new model to `prices.json`. Admin only. |

Admin endpoints take `Authorization: Bearer $ADMIN_TOKEN`.

## How a record is judged

The Worker applies these checks in order. The CLI runs the same checks before sending, using `tokencount/policy.py`.

1. **Schema v0.1.**
2. **Consistency.**
   - `turns` and `subagent_calls` match the calls.
   - `peak_context_tokens` matches the calls.
   - Thinking tokens do not exceed output tokens.
3. **Hard limits** from `policy.json`.
   - Context and output token counts no model allows.
   - More than 5,000 calls.
   - A date before 2023 or in the future.
4. **Screening of the description** for e-mail addresses, URLs, file paths, API keys and long secret-like strings. The prompt, if the contributor chose to include it, is checked for e-mail addresses and secrets only.
5. **Soft flags**: a model missing from `prices.json`, a task costing more than $1,000, or more than 2,000 turns. A flagged record is stored but excluded from `/v1/summary` and `/v1/export` until it is reviewed.

A record that fails any of steps 1–4 is rejected and not stored.

Re-submitting the same `record_id` updates the stored record, which is how labels change.

## Identity and limits

- The server never stores GitHub usernames or tokens. A GitHub token is checked once against `api.github.com/user`, and only `HMAC(CONTRIBUTOR_SALT, user id)` is kept.
- Anonymous keys are hashed the same way.
- Client IP addresses are hashed too, and are used only for rate limiting.
- Daily limits:

  | Who | Records per day |
  |---|---|
  | Verified contributor | 10,000 |
  | Anonymous contributor | 2,000 |
  | Anonymous submissions from one IP address | 5,000 |

- **CPU budget.** Validating a 40-call record takes about 140 µs. The Workers free plan allows 10 ms of CPU per request, so the API accepts at most 100 records per request, and the CLI sends 25 at a time.

## Develop and test

Requires Node 22.18 or newer. The tests use Node's built-in test runner and `node:sqlite` as a stand-in for D1, so they run the real migration and the real SQL.

```bash
npm ci
```

```bash
npm test
```

```bash
npm run typecheck
```

To run the Worker locally, create `.dev.vars` (it is git-ignored) with these two lines:

```
CONTRIBUTOR_SALT=dev-salt
ADMIN_TOKEN=dev-admin
```

Then:

```bash
npm run migrate:local
```

```bash
npm run dev
```

Submit to the local Worker from the repository root:

```bash
python3 -m tokencount submit --api http://127.0.0.1:8787
```

## Deploy

You need a Cloudflare account. The free plan is enough to start.

1. Log in:

   ```bash
   npx wrangler login
   ```

2. Create the database:

   ```bash
   npx wrangler d1 create tokencount
   ```

   Then copy the printed `database_id` into `wrangler.toml`.

3. Apply the migration:

   ```bash
   npm run migrate:remote
   ```

4. Set the two secrets:

   ```bash
   openssl rand -hex 32 | npx wrangler secret put CONTRIBUTOR_SALT
   ```

   ```bash
   openssl rand -hex 32 | npx wrangler secret put ADMIN_TOKEN
   ```

   Keep `CONTRIBUTOR_SALT` fixed once records exist: changing it changes every contributor ID.

5. Deploy, and note the URL Wrangler prints:

   ```bash
   npm run deploy
   ```

6. Connect the rest of the project to that URL:
   - Set the repository variable `TOKENCOUNT_API_URL` to it. The nightly `dataset-snapshot` workflow then starts publishing.
   - Set `DEFAULT_API_URL` in `tokencount/remote.py` to it, so `tokencount submit` works without `--api`.
