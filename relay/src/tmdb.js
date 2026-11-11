// GET /v1/tmdb/<question>?<parameters>: TMDB's answer, asked with the relay's own TMDB key, so the
// people using SyncWatch don't each need one (TMDB asks every key's owner for a name and address).
//
// Only the questions SyncWatch's Ideas window asks, with only their parameters (tmdb.json); anything
// else is refused, so this is no open door to TMDB or anywhere else. At most so many questions per
// minute from one address, and for everyone together (TmdbLimit). The key is a Cloudflare secret
// (TMDB_KEY: the 32-character "API Key" or the long "API Read Access Token"); it's never in the code
// or in an answer. Nothing is logged.
//
// Answers: 200 with TMDB's JSON, or JSON {"error": ...}:
//   400 parameter (not allowed / doesn't match / missing), 404 question (not one of them) or missing
//   (TMDB has no such film), 405 method, 429 busy (too many questions; try again in a minute),
//   502 key (TMDB refused the relay's key) / network / tmdb / big, 503 nokey (the relay has no key).

import { DurableObject } from "cloudflare:workers";
import { take } from "./bucket.js";
import RULES from "./tmdb.json";

const API = "https://api.themoviedb.org/3";
const ROUTES = RULES.routes.map((route) => ({ ...route, pattern: new RegExp(route.path) }));
const PARAMS = new Map(Object.entries(RULES.params).map(([name, rule]) => [name, new RegExp(rule)]));
const V3_KEY = /^[0-9a-fA-F]{32}$/;

function json(status, body, headers = {}) {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", ...headers } });
}

// For tests in Cloudflare's local runtime only: TMDB_BASE may point at a stand-in on this computer.
function base(env) {
  const wanted = (env.TMDB_BASE || "").trim();
  return /^http:\/\/(127\.0\.0\.1|localhost):\d{2,5}(\/[\w/.-]*)?$/.test(wanted) ? wanted.replace(/\/+$/, "") : API;
}

export async function tmdb(request, env, url) {
  if (request.method !== "GET") {
    return json(405, { error: "method" });
  }
  const question = url.pathname.slice("/v1/tmdb".length);
  const route = ROUTES.find((candidate) => candidate.pattern.test(question));
  if (route === undefined) {
    return json(404, { error: "question" });
  }
  const params = new URLSearchParams();
  for (const [name, value] of url.searchParams) {
    const rule = PARAMS.get(name);
    if (rule === undefined || params.has(name) || !rule.test(value)) {
      return json(400, { error: "parameter", name: name.slice(0, 40) });
    }
    params.set(name, value);
  }
  for (const name of route.required) {
    if (!params.has(name)) {
      return json(400, { error: "parameter", name });
    }
  }
  const key = (env.TMDB_KEY || "").trim();
  if (!key) {
    return json(503, { error: "nokey" });
  }
  const address = request.headers.get("CF-Connecting-IP") || "unknown";
  const limiter = env.TMDB_LIMITS.get(env.TMDB_LIMITS.idFromName("tmdb"));
  const verdict = await limiter.fetch("https://limit/take?address=" + encodeURIComponent(address));
  if (verdict.status !== 200) {
    return json(429, { error: "busy" }, { "Retry-After": "60" });
  }

  const headers = { Accept: "application/json", "User-Agent": "SyncWatch relay" };
  if (V3_KEY.test(key)) {
    params.set("api_key", key);
  } else {
    headers.Authorization = "Bearer " + key;
  }
  let answer;
  try {
    answer = await fetch(base(env) + question + "?" + params.toString(), {
      headers,
      cf: { cacheEverything: true, cacheTtlByStatus: { "200-299": route.cache_s, "300-599": 0 } },
    });
  } catch (error) {
    return json(502, { error: "network" });
  }
  if (answer.status === 401 || answer.status === 403) {
    return json(502, { error: "key" });
  }
  if (answer.status === 404) {
    return json(404, { error: "missing" });
  }
  if (answer.status === 429) {
    return json(429, { error: "busy" }, { "Retry-After": "60" });
  }
  if (!answer.ok) {
    return json(502, { error: "tmdb", status: answer.status });
  }
  const body = await answer.arrayBuffer();
  if (body.byteLength > RULES.max_answer_bytes) {
    return json(502, { error: "big" });
  }
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

// One object for all TMDB questions: a bucket per address and one for everyone. Kept in memory only:
// if Cloudflare evicts the object while it's idle they simply start full again, which is fine here.
// TMDB_PER_MINUTE / TMDB_BURST (per address) may be set for tests.
export class TmdbLimit extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    const limits = RULES.limits;
    this.rate = (Number(env.TMDB_PER_MINUTE) || limits.per_address_per_minute) / 60;
    this.burst = Number(env.TMDB_BURST) || limits.per_address_burst;
    this.allRate = limits.all_per_minute / 60;
    this.allBurst = limits.all_burst;
    this.all = { tokens: this.allBurst, at: Date.now() };
    this.addresses = new Map();
  }

  async fetch(request) {
    const address = new URL(request.url).searchParams.get("address") || "";
    let bucket = this.addresses.get(address);
    if (bucket === undefined) {
      if (this.addresses.size >= 20000) {
        this.addresses.clear(); // a flood of addresses: start over rather than grow without end
      }
      bucket = { tokens: this.burst, at: Date.now() };
      this.addresses.set(address, bucket);
    }
    const allowed = take(bucket, this.rate, this.burst) && take(this.all, this.allRate, this.allBurst);
    return new Response(null, { status: allowed ? 200 : 429 });
  }
}
