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
  enables the plugin and points it at the daemon (secret from env
  `SCHEDULER_API_SECRET`).
- `Dockerfile` — package image (`sanarena/roundcube-scheduler`):
  `/pkg/{plugins,overrides}`, installed by a webmail initContainer
  like the hellomail package.

## Checks

- `php -l` on all PHP files (pod PHP, local php is broken), `node
  --check` on the JS. No PHP unit harness in the repo — covered by
  live E2E instead (short snooze/schedule round-trips).
