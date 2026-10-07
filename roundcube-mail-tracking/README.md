# Roundcube `mail_tracking` plugin + package image

Mailbutler-like tracking suite inside webmail (Roundcube 1.6 /
Elastic, hellomail-compatible), backed by the `mail-tracker`
daemon's service API (shared secret + asserted user, owner-scoped
server-side). The plugin never sees mailbox passwords.

## Behavior

- **Sent list** subjects get tracking badges (opened/clicked) via
  the `messages_list` hook: one bulk IMAP `MESSAGE-ID` fetch per
  render (index headers only carry `mid:<seq>` synthetics) + one
  batched `POST /api/by-mids` lookup. The badge rides on the
  subject cell because widescreen renders a hardcoded column set.
- **Sent message view** gets a Tracking header row with counts and a
  Details link into the Tracking task.
- **Tracking task** (app menu): stats cards (sent/opened/opens/
  clicks/open rate), tracked messages with paging, per-message
  event timeline (open/click, time, city/country, client, link),
  per-message tracking on/off, and do-not-track management (whole
  mailbox + per-recipient).
- **Compose** gets a "Track opens and clicks" switch (default on),
  styled like the native message-option switches, first in the options list.
  Unchecking sets `X-Tracker-Track: no` in both `message_ready`
  (so send-later captures carry it regardless of plugin order) and
  `message_before_send` (final send). Checked sends no header, so
  server-side opt-outs keep applying (unlike the tracker's explicit
  `yes`, which forces).

## Files

- `plugins/mail_tracking/` — the plugin (PHP + JS + localization +
  Elastic CSS + task template). Stable core APIs only:
  `register_task`, `messages_list`, `message_headers_output`,
  `message_ready`, `message_before_send`, `http_post`/command JSON.
- `overrides/tracking.inc.php` — auto-included by mailu start.py;
  enables the plugin and points it at the daemon (secret from
  `/overrides/tracking-secret.key`, rendered by the initContainer —
  php-fpm `clear_env` wipes env).
- `Dockerfile` — package image (`sanarena/roundcube-tracking`):
  `/pkg/{plugins,overrides}`, installed by a webmail initContainer
  like the hellomail/scheduler packages.

## Roundcube integration notes (all learned live)

- Index headers carry synthetic `mid:<seq>` IDs: the list fetch
  does not include MESSAGE-ID, so the hook resolves real IDs with
  one bulk `UID FETCH ... BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]`
  per Sent render (`$storage->conn` is public). Never look up
  `mid:` synthetics server-side.
- Widescreen (the default layout) renders a hardcoded column set
  (`listcols_widescreen` env + template), so plugin-added columns
  never display there. Badges ride on the subject cell instead,
  badge-first because subjects truncate.
- Message view headers DO carry the real Message-ID (full fetch),
  with a single-UID `fetch_mids` fallback for cached/synthetic
  cases.
- Roundcube logs to the `stdout` driver here, which php-fpm
  swallows — hook failures are silent in container logs. Debug by
  encoding markers into list cells and reading the list JSON.
- Compose toggle: custom-switch default-on + hidden `_track`; switch off
  sends `X-Tracker-Track: no` in both `message_ready` (send-later
  capture order) and `message_before_send`. Checked sends no
  header so server opt-outs apply.

## Checks

- `php -l` on all PHP files (pod PHP, local php is broken), `node
  --check` on the JS. No PHP unit harness in the repo — covered by
  live E2E instead (badges, task views, compose toggle, opt-outs,
  cross-user isolation).
