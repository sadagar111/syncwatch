# SyncWatch relay

A tiny server that passes messages between people in the same SyncWatch room.
It runs for free on Cloudflare Workers and forwards only; the messages are
encrypted and signed by the apps (docs/ROOMS.md), so it can't read or fake any.

- `src/index.js`: the whole relay (about 150 lines)
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

## Running it on your PC

Needs Node.js. In this folder:

```
npm ci
npx wrangler dev --port 8787
```

Then `SYNCWATCH_TEST_RELAY=http://127.0.0.1:8787 python -m unittest tests.test_relay.RelayConformance`
from the project folder checks it. `tools/local_relay.py` is a Python copy of the
same behaviour that needs nothing installed (used by the normal tests).
