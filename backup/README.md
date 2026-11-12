# Mail backup: per-mailbox copy to your own IMAP account

Each mailbox can set a backup target (any IMAP server over TLS, e.g.
Gmail with an app password) under **webmail → Settings → Backup**
(the `mail_backup` plugin in `roundcube-mail-backup/`). This daemon
then keeps an append-only copy of the whole mailbox there.

What gets copied:

- Every folder, including Sent, Drafts, Archive, Scheduled, Snoozed and
  Trash. Junk is skipped unless "Also back up Junk" is ticked.
- Folders land below a prefix, default `Backup/<mailbox>`, so on Gmail
  you get labels like `Backup/admin@arminnikdel.com/Sent`.
- Read/answered/flagged/draft flags and the original received date are
  kept.
- **Append-only:** mail deleted on the server stays in the backup.
  Messages are identified by the SHA-256 of their bytes, so a message
  moved between folders (INBOX → Archive) is copied once, into the folder
  where it was first seen.
- The catch-all lands in admin@, so admin@'s target covers catch-all
  mail too.

## How it works

1. Every `POLL_SECONDS` (default 300), and right after Save or **Back
   up now**, the poller runs each enabled mailbox.
2. **Source:** Mailu dovecot over trusted-net IMAP. Dovecot accepts any
   password from the pod network, the same mechanism `mail-scheduler`
   uses. Folders are opened read-only (`EXAMINE`) and messages read with
   `BODY.PEEK[]`, so nothing changes on the server.
3. **Walking a folder:** each folder is walked by UID from the last
   position, which resets when UIDVALIDITY changes (the hash dedupe
   prevents duplicates). Progress is committed per message.
4. **Budgets:** each run copies at most `MAX_MESSAGES_PER_RUN` (500) or
   `MAX_BYTES_PER_RUN` (300 MB). A big first backup continues
   automatically 15 s later until it has caught up.
5. **Refusals by the target:**
   - Over quota, throttling or "try again" (`OVERQUOTA`, `LIMIT`,
     `THROTTLED`, …) stop the run without advancing, and the next run
     retries. Nothing is lost.
   - A message refused for good, or larger than `MAX_MESSAGE_BYTES`
     (35 MB), is skipped and counted in the status.
   - `TRYCREATE` re-creates the folder and retries.

## Security

- **API:** ClusterIP only, shared secret `X-Backup-Secret` (Secret
  `backup-api`, also rendered into webmail by its initContainer). Owner
  scoping as in the scheduler: the plugin passes the logged-in user, and
  every read or change is scoped to that user.
- **Target password:** write-only through the API, stored
  Fernet-encrypted with `CRYPT_KEY` (Secret `backup-crypt`). If that key
  is lost, users only need to re-enter their password.
- **Target vetting:**
  - Implicit TLS with certificate and hostname verification.
  - The hostname must resolve only to public addresses, and the
    connection is pinned to the vetted IP. A webmail user therefore
    cannot point the daemon at cluster services or the LAN.
- Test-only env knobs `ALLOW_PRIVATE_TARGETS` and `INSECURE_TLS` must
  never be set in production.

## Layout

- `mbackup/config.py`: env config.
- `mbackup/db.py`: SQLite store (targets, per-folder UID state,
  copied-message hashes).
- `mbackup/crypto.py`: Fernet box for target passwords.
- `mbackup/imaputil.py`: quoting, modified UTF-7, LIST/FETCH parsing,
  target vetting, the pinned TLS connection.
- `mbackup/sync.py`: the sync engine.
- `mbackup/server.py`: HTTP API, poller, main.
- `tests/`: 41 unit tests with an in-memory IMAP fake.
  `docker build --target test backup` runs them (needs `cryptography`).
- `k8s/`: manifests, mirrored into the Flux repo `mailu/` dir.

## API (`/healthz` is open; everything else needs `X-Backup-Secret`)

- `GET /v1/target?user=` → status and target, never the password.
- `PUT /v1/target` `{user, host, port, username, password?, prefix,
  enabled, include_junk}`. An empty or absent password keeps the stored
  one. Changing host, port, username or prefix starts a fresh full
  backup.
- `DELETE /v1/target?user=`
- `POST /v1/test` `{user, host, port, username, password?}` → `{ok,
  message}`. Uses the stored password when none is given; rate-limited
  to one call every 3 s per user.
- `POST /v1/run` `{user}` → queues an immediate run.

## Build and deploy (arm64, Pi build host)

```bash
docker build --target test backup
docker build -t sanarena/mail-backup:<ver> backup && docker push sanarena/mail-backup:<ver>
```

Pin the digest in the Flux repo `mailu/backup-deployment.yaml`.

**Verified 2026-11-12 (1.0.0):**

- Integration test on the Pi: two GreenMail servers plus the test
  webmail, driven through Settings → Backup.
  - Wrong password → clear error.
  - Saving starts the backup, which catches up across budgeted runs.
  - INBOX, Sent and Archive copied with flags; Junk skipped.
  - New mail copied once; a moved message not duplicated.
  - A validation error is shown and nothing is saved.
- In the cluster: dovecot read works and imap.gmail.com TLS verifies.
