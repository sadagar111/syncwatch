// The SyncWatch relay: passes messages between everyone in the same room.
//
// Everything that goes through here is already encrypted and signed by the
// SyncWatch apps (see docs/ROOMS.md), so this server can't read or change
// anything, and it doesn't need to be trusted. It stores nothing and logs
// nothing; it only forwards.
//
//   GET  /v1/health        -> {"ok": true, "service": "syncwatch-relay", "version": 1}
//   GET  /v1/ch/<32 hex>   WebSocket: join a channel (a room, or someone's mailbox).
//                          Each binary message goes to everyone else on the channel.
//   POST /v1/ch/<32 hex>   Deliver the request body to everyone on the channel now
//                          (how friends' apps leave invitations in your mailbox).
//
// Limits, the same as tools/local_relay.py (tests/test_relay.py checks both):
// messages up to 64 KiB, binary only, 32 connections per channel,
// 20 messages per second per connection (bursts of 60; extra ones are dropped),
// 5 posts per second per channel (bursts of 20).
//
// Each channel is a Durable Object. It uses the "hibernation" WebSocket API,
// so an idle room costs nothing while people are connected.

import { DurableObject } from "cloudflare:workers";

const MAX_MESSAGE = 64 * 1024;
const MAX_CONNECTIONS = 32;
const RATE_PER_S = 20;
const BURST = 60;
const POST_RATE_PER_S = 5;
const POST_BURST = 20;
const CHANNEL_PATH = /^\/v1\/ch\/([0-9a-f]{32})$/;

function text(status, body) {
  return new Response(body + "\n", { status, headers: { "Content-Type": "text/plain" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/v1/health") {
      return Response.json({ ok: true, service: "syncwatch-relay", version: 1 });
    }
    const match = CHANNEL_PATH.exec(url.pathname);
    if (!match) {
      return text(404, "not found");
    }
    const channel = env.CHANNELS.get(env.CHANNELS.idFromName(match[1]));
    if (request.method === "POST") {
      // Read the whole (small) body here, so the channel object always gets a complete one.
      const body = await readBody(request);
      if (body === null) {
        return text(413, "too big");
      }
      return channel.fetch(new Request(request.url, { method: "POST", body }));
    }
    return channel.fetch(request);
  },
};

// The request body, or null if it's larger than a message may be (then the rest is discarded).
async function readBody(request) {
  if (request.body === null) {
    return new Uint8Array(0);
  }
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    size += value.byteLength;
    if (size > MAX_MESSAGE) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const body = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return body;
}

// Token bucket: `rate` per second on average, at most `burst` at once.
function take(bucket, rate, burst) {
  const now = Date.now();
  bucket.tokens = Math.min(burst, bucket.tokens + ((now - bucket.at) / 1000) * rate);
  bucket.at = now;
  if (bucket.tokens < 1) {
    return false;
  }
  bucket.tokens -= 1;
  return true;
}

export class Channel extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    // Kept in memory only; if Cloudflare evicts the object while it's idle these
    // simply start fresh, which is fine for rate limits.
    this.buckets = new Map();
    this.postBucket = { tokens: POST_BURST, at: Date.now() };
  }

  async fetch(request) {
    if (request.method === "POST") {
      return this.post(request);
    }
    if (request.method !== "GET") {
      return text(405, "method not allowed");
    }
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return text(426, "expected a WebSocket");
    }
    if (this.ctx.getWebSockets().length >= MAX_CONNECTIONS) {
      return text(503, "channel full");
    }
    const pair = new WebSocketPair();
    this.ctx.acceptWebSocket(pair[1]);
    return new Response(null, { status: 101, webSocket: pair[0] });
  }

  async post(request) {
    const body = await request.arrayBuffer();
    if (body.byteLength > MAX_MESSAGE) {
      return text(413, "too big");
    }
    if (body.byteLength === 0) {
      return text(400, "empty");
    }
    if (!take(this.postBucket, POST_RATE_PER_S, POST_BURST)) {
      return text(429, "slow down");
    }
    this.broadcast(body, null);
    return new Response(null, { status: 202 });
  }

  webSocketMessage(ws, message) {
    if (typeof message === "string") {
      ws.close(1003, "binary only");
      return;
    }
    if (message.byteLength > MAX_MESSAGE) {
      ws.close(1009, "message too big");
      return;
    }
    let bucket = this.buckets.get(ws);
    if (bucket === undefined) {
      bucket = { tokens: BURST, at: Date.now() };
      this.buckets.set(ws, bucket);
    }
    if (!take(bucket, RATE_PER_S, BURST)) {
      return; // flooding: dropped
    }
    this.broadcast(message, ws);
  }

  broadcast(message, sender) {
    for (const ws of this.ctx.getWebSockets()) {
      if (ws !== sender) {
        try {
          ws.send(message);
        } catch (error) {
          // that connection is going away; nothing to do
        }
      }
    }
  }

  webSocketClose(ws, code, reason) {
    this.buckets.delete(ws);
    try {
      ws.close(code, reason);
    } catch (error) {
      // already closed
    }
  }

  webSocketError(ws) {
    this.buckets.delete(ws);
  }
}
