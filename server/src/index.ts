import { handle, type Database } from "./app.ts";

export interface Env {
  DB: Database;
  /** HMAC key for pseudonymous contributor IDs. `wrangler secret put CONTRIBUTOR_SALT` */
  CONTRIBUTOR_SALT?: string;
  /** Bearer token for /v1/admin/*. `wrangler secret put ADMIN_TOKEN` */
  ADMIN_TOKEN?: string;
}

async function githubUserId(token: string): Promise<number | null> {
  const res = await fetch("https://api.github.com/user", {
    headers: { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json", "User-Agent": "tokencount-api" },
  });
  if (!res.ok) return null;
  const user = (await res.json()) as { id?: unknown };
  return typeof user.id === "number" ? user.id : null;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (!env.CONTRIBUTOR_SALT) {
      return new Response(JSON.stringify({ error: "Server is not configured (CONTRIBUTOR_SALT missing)." }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    return handle(req, {
      db: env.DB,
      salt: env.CONTRIBUTOR_SALT,
      adminToken: env.ADMIN_TOKEN,
      githubUserId,
      now: () => new Date(),
    });
  },
};
