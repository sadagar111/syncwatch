# SyncWatch relay

A tiny server that passes messages between people in the same SyncWatch room.
It runs for free on Cloudflare Workers and forwards only; the messages are
encrypted and signed by the apps (docs/ROOMS.md), so it can't read or fake any.

- `src/index.js`: the relay itself (about 150 lines)
- `src/tmdb.js`, `src/tmdb.json`: since 0.5.1, a few TMDB questions for the Ideas window, asked with
  the relay's own TMDB key, so friends don't each need one (see "TMDB key" below)
- `wrangler.jsonc`: Cloudflare settings
- `package.json` / `package-lock.json`: the exact version of Cloudflare's `wrangler` tool

## Deploying

Two ways, both in docs/GIT_GUIDE.md, lesson 5:

- **Cloudflare builds it from the repository** (Workers Builds): Worker name
  `syncwatch-relay` (must match `wrangler.jsonc`), root directory `relay`,
  deploy command `npx wrangler deploy`.
- **GitHub deploys it**: `.github/workflows/relay.yml` deploys whenever `relay/`
  changes on `main`, once the secrets are set up, and prints the address in the
  run's summary.

## TMDB key (since 0.5.1)

SyncWatch apps without a TMDB key of their own ask TMDB through the relay, with one key for
everyone. The key is a Cloudflare **secret**, never in the repository or the app:

- Cloudflare dashboard: Workers & Pages > `syncwatch-relay` > Settings > Variables and Secrets >
  Add > type **Secret**, name `TMDB_KEY`, value: your TMDB key (the long "API Read Access Token" or
  the 32-character "API Key" from themoviedb.org > Settings > API). Save; it applies at once and
  stays through later deploys.
- Or, in this folder: `npx wrangler secret put TMDB_KEY` (it asks for the value).

Without it, the relay answers TMDB questions with "nokey" and the apps say a key is needed. Only the
three questions in `src/tmdb.json` are passed on, with only their parameters; each address may ask
60 per minute (120 at once) and everyone together 600 per minute. Nothing is logged.

## Running it on your PC

Needs Node.js. In this folder:

```
npm ci
npx wrangler dev --port 8787
```

Then `SYNCWATCH_TEST_RELAY=http://127.0.0.1:8787 python -m unittest tests.test_relay.RelayConformance tests.test_rooms`
from the project folder checks it, the second part with whole rooms of simulated people
(GitHub does the same for every change to the relay or the room code). `tools/local_relay.py` is a Python copy of the
same behaviour that needs nothing installed (used by the normal tests).
