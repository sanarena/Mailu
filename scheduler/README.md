# Mail scheduler: send-later + snooze wake-ups (stdlib-only Python).

Companion to the mail tracker. Powers scheduled sending and snoozing
for Roundcube webmail (the `mail_scheduler` plugin) and the tracker
dashboard compose box.

How it works:

1. A client (already-user-authenticated) POSTs a scheduled message
   (full MIME + envelope + `send_at`) or a snooze (Message-ID +
   folder + `wake_at`) to the internal API (ClusterIP only,
   shared-secret auth, every object owner-scoped).
2. Every `POLL_SECONDS` the poller submits due messages through the
   backend postfix (pod-network `permit_mynetworks` relay, no SASL
   needed) — so scheduled mail flows through the tracker milter,
   rspamd and DKIM exactly like a live send — and moves due snoozes
   back from `Snoozed` via trusted-net IMAP (any-password login).
3. After a scheduled send, the user's `Scheduled`-folder copy is
   filed to Sent (Gmail semantics); a snooze whose message vanished
   (user moved/deleted it) is silently completed.

## Layout

- `sched/config.py` - env config
- `sched/db.py` - SQLite store (scheduled, snoozes, retention purge)
- `sched/server.py` - HTTP API, poller, SMTP/IMAP workers, main
- `tests/` - unit tests (`python3 -m unittest discover -s
  scheduler/tests` from the repo root; SMTP/IMAP faked)
- `k8s/` - manifests (mirrored into the Flux repo `mailu/` dir)

## API (`/healthz` is open for probes; the rest need `X-Scheduler-Secret`)

- `POST /v1/scheduled` `{user, sender, recipients[], mime_b64,
  send_at}` → `{id, send_at}`
- `GET /v1/scheduled?user=` → owner-scoped list (no MIME bodies)
- `DELETE /v1/scheduled/:id?user=` → cancel a queued send
- `POST /v1/snoozes` `{user, message_id, folder, wake_at}` → `{id}`
- `GET /v1/snoozes?user=`, `DELETE /v1/snoozes/:id?user=`
- `GET /healthz`

Validation: sender must be local, 1–200 recipients, MIME ≤
`MAX_MIME_BYTES` and parseable, times within +30s..`MAX_FUTURE_DAYS`.
Failed sends retry with backoff (5m/30m/2h) and give up after 24h.

## Operations

- Build (arm64): `docker build -t sanarena/mail-scheduler:<ver>
  scheduler` on the Pi, then push.
- The API secret lives in the live-only `scheduler-api` Secret
  (`api-secret` key), shared read-only with the tracker and webmail
  pods; never in git.
