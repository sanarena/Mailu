# Roundcube `mail_backup` plugin

Adds **Settings → Backup** to webmail. Each mailbox can set an IMAP
backup target there (server, port, username, password, folder prefix,
"Also back up Junk"). The page also has **Test connection** and **Back
up now** buttons and a live status: state, last successful backup,
messages backed up, anything still to copy, and the last problem. The
copying itself is done by the `mail-backup` daemon (`backup/`).

## Files

- `plugins/mail_backup/`: the plugin.
  - PHP settings hooks and AJAX actions.
  - JS for the buttons and status polling (textContent only).
  - `en_US` labels.
  - Elastic CSS, including the section icon.
- `overrides/backup.inc.php`: auto-included by mailu start.py. It
  enables the plugin and points it at the daemon. The API secret is
  read from `/overrides/backup-secret.key`, which the webmail
  initContainer renders from the live-only `backup-api` Secret. It
  can't come from the environment, because php-fpm runs with
  `clear_env=yes`.
- `Dockerfile`: the package image (`sanarena/roundcube-backup`) with
  `/pkg/{plugins,overrides}`, installed by the `install-backup-plugin`
  initContainer like the scheduler and tracking packages.

## Notes

- The password field is write-only. Leaving it empty keeps the stored
  password, and the daemon never sends it back. App passwords may
  contain spaces, so the field is not trimmed.
- Clearing the IMAP server field and saving removes the backup target.
- Save errors from the daemon's validation are shown in Roundcube's
  message bar. The settings are not saved in that case.
- Settings forms render inside the preferences iframe. To drive the
  form in a browser test, open
  `?_task=settings&_action=edit-prefs&_section=backup&_framed=1`.

## Checks

- `php -l` on the PHP files (pod PHP) and `node --check` on the JS.
- Live end-to-end test through the Pi test harness; see
  `backup/README.md`.
