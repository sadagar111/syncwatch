# Rooms: how they work and why they're safe

This is the design behind watching together with several people (since version 0.4).
It's written for anyone who wants to check it, including people trying to break it.
Report weaknesses as described in SECURITY.md.

## The pieces

| Piece | Where | What it does |
|---|---|---|
| Relay server | `relay/` (Cloudflare Worker), `tools/local_relay.py` (same thing for tests) | Passes messages between everyone on the same *channel*. Stores and logs nothing. Not trusted with anything. |
| Identity | `syncwatch/room/identity.py` | Each PC's own keys, made on first start. |
| Room codes | `syncwatch/room/codes.py` | What the host shares. |
| Envelopes | `syncwatch/room/crypto.py` | Every message is encrypted and signed before it reaches the relay. |
| WebSocket client, channels | `syncwatch/net/websocket.py`, `syncwatch/net/relay.py` | The connection to the relay; reconnects by itself. |
| Room session | `syncwatch/room/session.py` | Hosting, asking to join, letting in, room keys, who's in. |
| Friends | `syncwatch/room/friends.py`, `syncwatch/room/store.py` | Friend list, mailboxes, what's remembered. |
| Message rules | `syncwatch/net/protocol.py` | Every message type with its allowed fields, sizes and ranges. |
| Watching rules | `syncwatch/sync/controller.py` | What each message does to playback; cooldowns and permissions. |

## Goals

1. The relay, and anyone watching the network or the relay, learns nothing but
   message sizes and timing. They can't read, change, invent or replay messages.
2. Knowing a room code only lets you **ask** to join. The host decides
   ("Ask every time" by default).
3. A leaked code doesn't let anyone read the room either.
4. Nobody can act as the host or as someone else, not even people inside the room.
5. Someone who left or was removed can't read along afterwards.
6. Shared things (later: film links) always show who really shared them.

Not a goal: hiding *that* a room exists from the relay operator (that's you), or
protecting against someone who controls a member's PC.

## Identity

On first start each PC makes two key pairs and stores them in the profile folder
(`identity.json`, unencrypted, like an SSH key without a passphrase):

- **Ed25519** for signing. Every message carries a signature.
- **X25519** for encryption to that person ("sealed" messages).

`fingerprint = SHA-256("SyncWatch identity v1\0" ‖ signing key ‖ encryption key)`.
People see 40 bits of it as a short ID like `K7QF-2M9X` next to the name. The app
compares the full 256 bits, so a lookalike short ID doesn't fool it; the short ID
is there so humans notice a stranger using a friend's name.

The same file holds a 16-byte *mailbox secret* (see Friends).

## Room codes

`SW-` + 44 base32 characters (Crockford alphabet, case-insensitive) =
version (1 byte) ‖ room secret (16 random bytes) ‖ host commitment (8 bytes) ‖ checksum (2 bytes).

- The **host commitment** is the first 8 bytes of the host's fingerprint. The app
  only accepts a host whose keys match it, so knowing the code isn't enough to
  impersonate the host (that would need a second preimage on 64 bits of SHA-256).
- The **checksum** turns typos into "check the code" instead of an empty room.
- If the host uses a relay other than the built-in one, `@https://…` follows.
- Each host has one permanent code; **New code** makes a new secret (the old code
  then leads nowhere).

From the code, with HKDF-SHA256 (salt `SyncWatch room v1`, input secret ‖ commitment):

- `channel` = 16 bytes (`info "channel"`), the relay channel, written as 32 hex characters;
- `lobby key` = 32 bytes (`info "lobby"`).

## Envelopes

```
version (1) | kind (1) | key field (8) | [direct: throwaway X25519 key (32)] | nonce (12) | ciphertext
```

Ciphertext = ChaCha20-Poly1305 over `sender signing key (32) ‖ Ed25519 signature (64) ‖ JSON body`,
with the associated data `"SyncWatch env v1\0" ‖ channel ‖ header`.
The signature covers `"SyncWatch msg v1\0" ‖ channel ‖ header ‖ body`.

| Kind | Key | Key field | Used for |
|---|---|---|---|
| lobby | lobby key (from the code) | zeros | `hello`, the host's `card` |
| room | the current *epoch key* | epoch id | everything in the room |
| direct | X25519(throwaway, recipient) → HKDF | recipient's key id | asking to join, letting in, new epoch keys, mailboxes |

Because the channel and header are bound into both the encryption and the
signature, a message can't be moved to another room, another epoch or another
recipient. Every body carries the sender's random session id `s` and a counter
`n`; receivers drop anything not newer than what they've seen from that
sender and session (`ReplayGuard`).

## Joining a room

1. The joiner connects to the channel and sends `hello` (lobby).
2. The host answers with a `card` (lobby, signed): name, encryption key, app and
   protocol version, and a fresh random `challenge` for this hosting session. The
   joiner checks the host's keys against the commitment in the code; a card that
   doesn't match is ignored and reported (SW-408).
3. The joiner sends `knock` as a direct message to the host: their name, keys,
   app version, the `challenge`, and a random `nonce`. Without the current
   challenge a recorded knock is useless.
4. The host's app decides, in this order:
   - blocked → turned away (`denied`);
   - already in the room (their SyncWatch restarted) → in;
   - room full (*Most people in my room*, 10 by default, 2–20) → `full`;
   - in this room within the last 10 minutes → in, without asking (*Let people
     who just dropped out back in*, on by default; recognised by their full
     fingerprint);
   - otherwise the setting *When someone asks to join* (`room.policy`):
     **Ask every time** (`ask`, default), *Friends join directly, ask the rest*
     (`friends_direct`), *Only friends* (`friends_only`; others get
     `friends_only` without asking), or (advanced) *Anyone with the code* (`open`).

   A different protocol version gets `version`. At most 25 requests wait at once
   (more get `busy`), each identity can knock at most 6 times a minute, and a
   request nobody answers expires after 5 minutes. A waiting joiner knocks again
   every minute so it doesn't expire, and says `hello` every 10 seconds so it
   notices a restarted host (new challenge).
5. Approved: the host makes a **new epoch key** and sends it
   - to the newcomer in `welcome` (direct; includes their `nonce`, so only the
     answer to *their* knock counts), with the member list;
   - to everyone else in `rekey` (direct, numbered so old ones can't be replayed).
   Turned away: `deny` (direct) with the reason.

The host rekeys on every join and every departure (left, timed out, removed).
The previous key is accepted for 5 seconds so messages in flight still
arrive, then forgotten. So whoever has a code but wasn't let in, and whoever
left, can't read the room. Everyone let in at the same moment (*Let all in*)
shares one new key.

**Coming back.** The host remembers who was in the room for 10 minutes (in
`rooms.json`), so after a dropped connection, a restart of either app, or a
**New code** (`moved`, sent directly to each member with the new code) people
get straight back in. People who left on purpose, were removed or were blocked
are forgotten at once, and closing the room on purpose forgets everyone.

## In the room

Room messages are accepted only from people on the host's member list (sent
with every new room key, and as `members` every 30 seconds). By kind:

| Who sends | Kinds | Who acts on them |
|---|---|---|
| host only | `members`, `close`, `state` (heartbeat with the rules), `cmd` (play/pause/jump for everyone), `reject`, `sync_prepare`, `sync_go`, `sync_abort`, `pong` | members (anything of these from a member is dropped) |
| members | `req` (please play/pause/jump), `sync_ready`, `ping`, `bye` | only the host |
| everyone | `profile` (name, picture ≤ 40 KB PNG/JPEG), `status` (heartbeat: position, playing, film) | everyone |

Members send *requests*; the host's app checks the rules (host-only control,
cooldowns per person, how often someone may jump) and then broadcasts the
command, or answers with `reject`. Editing your own copy of the app doesn't get
around that, because everyone else's app only follows commands from the host.

**Clock.** Each member pings the host; from the quickest round trips it
estimates the difference between its clock and the host's. Positions in `state`,
`status` and `cmd` carry the time on the host's clock, and Sync up starts
everyone at the same moment on the host's clock, so gaps are right even when
PC clocks differ.

**Liveness.** The host removes a member it hasn't heard from for 20 seconds
(they get back in without asking, see above); members show *Lost contact with
the host* after 15 seconds of silence and carry on when it's back.

## Friends

Each person has a **mailbox**: a relay channel derived from their mailbox secret
(HKDF, salt `SyncWatch mailbox v1`). Their app listens there while it runs.
Only friends learn the mailbox channel.

- **Becoming friends** happens in a room: *Add friend* sends `friend_req` (your
  name and mailbox) as a direct message; the other person answers `friend_ok`
  (theirs) or `friend_no`. If they answer after you've left the room, the
  `friend_ok` goes to your mailbox with their encryption key, and is accepted
  only if you really asked that exact person (requests expire after 10 minutes).
  Both then store each other's keys and mailbox.
- **Hosting**: when you host, your app posts a signed, sealed `hosting` message
  (your room code and name) into each friend's mailbox (`POST /v1/ch/<mailbox>`),
  repeats it every 5 minutes, and posts `hosting_end` when you stop (also when
  SyncWatch closes). A friend's room that isn't repeated disappears after
  12 minutes. A `hosting` message only counts if the code's host commitment is
  the sender's own: friends can only point to their own room.
- **On start** (and every 10 minutes), your app posts `who_hosts` to each
  friend's mailbox; friends who host answer with `hosting`. Nothing else is
  shared: friends don't see when you're merely online.
- Mailbox messages must be signed by a friend and carry a time within 10 minutes
  of the receiver's clock (`ts`), plus the usual replay counters. Anything else
  is dropped unread. Joining from the friend list still goes through the host's
  approval like any other join.
- Removing a friend posts `unfriend`, so their app forgets you too.
- *Tell friends when I'm hosting* (`friends.announce`) and *Show friends' rooms*
  (`friends.show_rooms`) switch the two halves off.

## The relay

`relay/src/index.js` on Cloudflare Workers (free plan), one Durable Object per channel:

- `GET /v1/ch/<id>` WebSocket: each binary message goes to everyone *else* on the channel;
- `POST /v1/ch/<id>`: one message to everyone on the channel (mailboxes);
- limits: 64 KiB per message, binary only, 32 connections per channel, 20 messages
  per second per connection (bursts of 60), 5 posts per second per channel.

It never sees keys or contents. What it could do if it were malicious: drop or
delay messages, or tell who connects when. It can't make an app accept anything,
because everything is signed and checked.

## What a stranger can still do

- With a code: see that a room exists and how busy it is; ask to join (the host
  sees the request and can block them, switch to *Only friends*, or make a new code).
- Flood the relay channel: the relay limits each connection, and apps drop
  anything that doesn't open with a key they hold before doing any other work.
- Without a code: nothing. Channels are 128-bit random names.
