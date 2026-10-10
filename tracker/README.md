# Mail Tracker (Mailbutler-like open/click tracking)

Server-side email tracking for every account on this Mailu setup,
inspired by Mailbutler's tracking. It is not a copy of Mailbutler
(proprietary SaaS with send-later, snooze, templates, tasks, ...):
this service covers the tracking core - open/click tracking with
who/when/where plus a dashboard - implemented at the mail-server
level so it works for **every** sending path: webmail, desktop and
phone mail apps (SMTP submission) and the panel compose box.

How it works:

1. A **milter** chained into postfix (`smtpd_milters`, before rspamd)
   sees every message. Outbound mail from local domains gets a
   tracking pixel injected and its links rewritten to pass through
   the click redirector. Rspamd then scans and DKIM-signs the
   already-tracked body, so signatures stay valid.
2. When a recipient opens the mail, their client fetches the pixel;
   the tracker logs timestamp, IP, GeoIP city/country and the
   parsed mail client/device. Link clicks redirect (302) and log.
3. The **Roundcube suite** (`mail_tracking` plugin) shows sent
   mail with open/click badges, per-email activity timelines,
   opt-out settings and a compose toggle. It authenticates with a
   shared service secret plus the asserted mailbox user; every
   route is owner-scoped server-side. (The standalone dashboard at
   `https://track.arminnikdel.com` was retired; the host keeps
   serving pixel/click URLs from already-sent mail.)

Stdlib-only Python, no dependencies. SQLite storage, offline GeoIP.

## Tracking rules (precedence)

1. Safety skips (never tracked, cannot be overridden): bounces,
   non-local senders, auto-replies (`Auto-Submitted`), S/MIME or
   PGP signed/encrypted mail, oversize mail (> `MAX_MIME_BYTES`),
   > `MAX_RECIPIENTS` recipients, unparseable or already-tracked
   bodies, mails without any usable text part.
2. `X-Tracker-Track: no` header (webmail compose toggle, power users).
3. Sender opt-out list, 1:1 recipient opt-out list.
4. `X-Tracker-Track: yes` overrides the opt-out lists (explicit
   per-message choice wins).
5. Otherwise: track.

Per-recipient honesty: one MIME body is shared by all recipients of
a message, so the pixel is per-message (exactly like Mailbutler for
multi-recipient mail). Opens are logged with IP/device/geo/time but
cannot be attributed to one recipient when several received the
mail; recipient opt-outs are fully honored for 1:1 mail.

Gmail and Apple Mail proxy or prefetch images, so "where" often
resolves to a provider datacenter - the same caveat every tracker
(including Mailbutler) has. The dashboard labels proxied opens.

## Layout

- `mail_tracker/config.py` - env config
- `mail_tracker/db.py` - SQLite store (messages, recipients, links,
  events, opt-outs, retention purge)
- `mail_tracker/mime.py` - pixel injection + link rewriting
- `mail_tracker/milter.py` - sendmail milter v6 protocol server
- `mail_tracker/web.py` - pixel/click endpoints + service JSON API
  (Roundcube plugin auth, owner-scoped; no login, no admin)
- `mail_tracker/server.py` - processor, HTTP adapter, janitor, main
- `mail_tracker/geo.py` + `tools/build_geo.py` - offline GeoIP
- `mail_tracker/ua.py` - client/device sniffing
- `tests/` - 96 unit tests (`python3 -m unittest discover -s tracker/tests`
  from the repo root)
- `k8s/` - manifests (also mirrored into the Flux repo `mailu/` dir)

## Configuration

See `.env.example`. Required secrets: `HMAC_SECRET` (link
signatures) and `TRACKER_API_SECRET` (Roundcube service auth) —
generate random 32-byte hex; in k8s they live in the live-only
`tracker-secrets` and `tracker-api` Secrets, never in git.

`DRY_RUN=true` parses and logs every message but never modifies mail
or writes rows - the safety ramp for first deploy.

## Roundcube suite (primary UI) + service API

The `mail_tracking` Roundcube plugin (`../roundcube-mail-tracking/`)
is the primary UI: Sent-list badges, per-message Tracking row,
stats/messages/detail/opt-outs task, and a compose toggle. It talks
to this daemon over the internal service API: requests carry
`X-Tracker-Secret: <TRACKER_API_SECRET>` (live-only `tracker-api`
Secret, never in git) plus an asserted `user`, always owner-scoped
server-side - service callers can never act as admin.

Service routes (all owner-scoped, all need the service secret):
`GET /api/stats`, `GET /api/messages`, `GET /api/messages/<id>`,
`POST /api/by-mids`, `GET /api/optouts`, `POST /api/optouts`
(self scope enforced), `POST /api/messages/<id>/tracking`.
Everything else under `/api/` 404s; unauthenticated calls 401.

`POST /api/by-mids {"user": ..., "message_ids": [1..200 MIDs]}`
returns `{results: {mid: {id, opens, clicks, last_open,
tracking}}}`; unknown or other users' MIDs are omitted (no leak).

The standalone dashboard (static SPA, mailbox-password login,
`/api/me`, `/api/compose`, `/api/optouts/check`, admin views) was
retired once the Roundcube suite was verified; its routes 404.
Send-later lives in webmail (scheduler plugin + `mail-scheduler`
daemon) and never touched this API again.

## GeoIP data

City/country comes from [DB-IP Lite](https://db-ip.com/db/lite.php)
(free monthly CSV, data (c) DB-IP, CC-BY-4.0 - attribution kept
here as required). The Dockerfile downloads it at build time and
`tools/build_geo.py` converts it to the compact `location.dat`
shipped in the image (`python3 tools/build_geo.py <csv.gz> location.dat`
before `docker build`; the 182MB `location.dat` is a gitignored build
input). Refresh by reconverting the current month and rebuilding, or
by mounting a converted file at `GEO_DB_PATH`. The reader mmaps the
file, so lookups cost kilobytes of RAM.

Without a database file the tracker still works; lookups return
unknown and IP/client are still logged.

## Operations

- Build (arm64, matches the cluster): on the Pi build host
  (`DOCKER_HOST=tcp://192.168.178.85:2376`)
  `docker build -t sanarena/mailu-tracker:<ver> tracker && docker push ...`.
  Verify `docker image inspect --format '{{.Architecture}}'`. Current
  live version: see `flux/mailu/tracker-deployment.yaml` image tag.
- Deploy: manifests live in the Flux repo `mailu/tracker-*.yaml`;
  push to `origin/main`, then `flux reconcile source git flux-system`
  and `flux reconcile kustomization mailu -n hodlticker`. Postfix
  picks up `smtpd_milters` from the overrides ConfigMap **at boot
  only** - after a ConfigMap change, `kubectl rollout restart
  deploy/mail-mailu-postfix` (a Flux sync alone does not restart it).
- Rollout: deploy with `DRY_RUN=true`, watch tracker logs for
  `tracked=yes/no reason=...` on live traffic, then set
  `DRY_RUN=false` and redeploy. Debug helpers: `MILTER_DEBUG=true`
  logs every milter packet; `HEADER_ONLY=true` adds headers without
  touching bodies.
- Bypass (tracker outage): postfix tempfails+retries while the
  milter is unreachable (same exposure class as rspamd, which is
  also in-path). To bypass instantly, remove the
  `tracker-postfix-overrides` mount from `mailu/helmrelease.yaml`
  and let Flux roll postfix.
- Retention: events and stored link URLs older than
  `RETENTION_DAYS` (default 365) are purged hourly; old messages
  are token-disabled so stale pixels stop logging.
- Rollback: delete the postfix override mount; previously tracked
  mail keeps working (pixels/logins resolve while the tracker runs).
- GeoIP needs true client IPs end to end: ingress Service
  `externalTrafficPolicy: Local`, Hetzner LB `uses-proxyprotocol`,
  and haproxy-ingress ConfigMap `proxy-protocol` listing the LB
  source CIDR (see the cluster runbook). Without all three, events
  log SNAT/LB addresses and geo stays empty.
- Milter wire notes (live incidents, encoded in `PostfixPhaseTest`
  and the chgheader tests - do not regress): postfix reads NO reply
  for macro/abort commands (answering desyncs the session and EOM
  actions are silently dropped); text-to-multipart conversion must
  flip `Content-Type` via `SMFIR_CHGHEADER` (`m`: index u32be, name
  NUL, value NUL, empty value deletes) since replbody cannot touch
  headers; keep all replbody chunks contiguous first
  (order `b,m,h,a`).
