# Roundcube `mail_scheduler` plugin + package image

Snooze and Send Later buttons for webmail (Roundcube 1.6 / Elastic,
hellomail-compatible), backed by the `mail-scheduler` daemon.

## Behavior

- **Snooze** (mail toolbar): pick Tonight / Tomorrow morning / Next
  Monday / custom time. Selected messages move to `Snoozed` and the
  daemon moves them back at wake time. **Wake now** cancels the
  wake-up and restores immediately; dragging a message out of
  `Snoozed` by hand also cancels (the daemon completes missing
  messages silently).
- **Send later** (compose toolbar): pick a time, the send is
  registered with the daemon (`message_before_send` captures the
  MIME and aborts live SMTP) and the copy is filed in `Scheduled`
  via the standard `_store_target` mechanism. The daemon submits at
  send time and files the copy to Sent. If the daemon is
  unreachable the compose form is kept and an error is shown —
  nothing is lost.

## Files

- `plugins/mail_scheduler/` — the plugin (PHP + JS + localization +
  Elastic CSS). Uses only stable core APIs: `register_action`,
  `add_button`, `message_before_send`, `show_popup_dialog`,
  `http_post`, storage folder/move calls.
- `overrides/scheduler.inc.php` — auto-included by mailu start.py;
  enables the plugin and points it at the daemon. The API secret is
  read from `/overrides/scheduler-secret.key`, rendered by the
  webmail initContainer from the live-only `scheduler-api` Secret —
  NOT from env: php-fpm runs with `clear_env=yes`, so `getenv()` in
  Roundcube never sees container env (verified live: env-based auth
  401s). start.py only includes `*.inc`/`*.inc.php`, so the raw
  `.key` file is never PHP-included.
- `Dockerfile` — package image (`sanarena/roundcube-scheduler`):
  `/pkg/{plugins,overrides}`, installed by a webmail initContainer
  like the hellomail package.

## Roundcube integration notes (all learned live)

- Compose guard: a custom toolbar command on a dirty compose form
  triggers the unsaved-changes "discard?" dialog instead of the
  handler unless the command is pushed to
  `rcmail.env.compose_commands` at init.
- `rcmail.command('send')` with undefined props throws in
  `command_handler` (`props.nocheck`); pass `''` like native buttons.
- `_store_target`: the visible "Save sent message in" select is
  rendered `noform` (display only); the submitted value lives in a
  hidden input synced on user change events. A bare `.value`
  assignment files to the stale folder — set both + trigger change.
  The hook also `ensure_folder(Scheduled)` first: core silently
  falls back to Sent when the target is missing.
- Toolbar links carry no `data-command` attr (only an `onclick`
  hook); query `a[onclick*="..."]` to relabel buttons.
- Snooze/Wake buttons are folder-scoped in JS: Snooze everywhere
  except inside `Snoozed`, Wake now only inside it.
- Mails without Message-ID report as `mid:<seq>` and are refused
  with a clear message (the daemon 400s them too).

## Checks

- `php -l` on all PHP files (pod PHP, local php is broken), `node
  --check` on the JS. No PHP unit harness in the repo — covered by
  live E2E instead (short snooze/schedule round-trips).
