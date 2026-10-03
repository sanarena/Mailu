# Mailu MCP — `feature/combined`: everything at once

Mail tools + full Mailu admin REST API + Gmail → Mailu copy jobs over MCP
(stdio), Python 3.9+ stdlib only — no third-party dependencies.

## Branches

| Branch | Contains | Tools |
|---|---|---|
| `feature/mail-mcp` | IMAP search/read + SMTP send | 7 `mail_*` |
| `feature/admin-mcp` | + admin REST API (domains, users, aliases, relays, tokens, alternatives, managers, random-alias) | + 43 `admin_*` |
| `feature/gmail-import` | + Gmail search + copy jobs | + 3 `gmail_*` |
| `feature/combined` (here) | all lanes + end-to-end smoke test | 53 total |

Branches are stacked (each builds on the previous) so every branch works
standalone; `feature/combined` is where all lanes are tested together.
Fork: `origin` = your fork, `upstream` = `Mailu/Mailu`.

## Admin tools (38)

Domains: `admin_domains_list`, `admin_domain_get/create/update/delete`,
`admin_domain_dkim` (rotate DKIM/DMARC keys), managers
(`admin_managers_list`, `admin_manager_add/check/remove`),
`admin_domain_users`, alternatives
(`admin_alternatives_list/get/create/delete`).
Users: `admin_users_list/get/create/update/delete` — every
`UserCreate`/`UserUpdate` parameter passes through (quota, forwarding,
autoreply, spam filter, …).
Aliases: `admin_aliases_list/get/create/update/delete`,
`admin_aliases_by_domain`.
Relays: `admin_relays_list/get/create/update/delete`.
Tokens: `admin_tokens_list/create`, `admin_tokens_of_user`,
`admin_user_token_create`, `admin_token_get/update/delete`.

Auth: `MAILU_API_BASE` + `MAILU_API_TOKEN` (Mailu `API_TOKEN`).

# Mailu MCP — `feature/gmail-import` (stacked on `feature/admin-mcp`)

Adds Gmail → Mailu copy jobs.

## Import tools (3)

| Tool | What it does |
|---|---|
| `gmail_search` | Run a Gmail search `query` (`max_results`, `label_ids`) → id/thread/subject/from/date/snippet |
| `gmail_import_run` | Copy job: every match `APPEND`ed verbatim (raw RFC822) to Mailu `dest_mailbox`; params `query`, `dest_mailbox`, `max_messages`, `dry_run`, `job_name`, `label_ids`, `mark_source_read`, `skip_existing` |
| `gmail_import_status` | Job state: copied total, last query, copied Gmail IDs |

Jobs are idempotent (state file at `~/.cache/mailu-mcp/import-state.json`,
override with `MAILU_IMPORT_STATE`); `dry_run` previews matches.
Gmail auth: `GMAIL_CLIENT_ID` + `GMAIL_CLIENT_SECRET` + `GMAIL_REFRESH_TOKEN`
(auto-refreshed), or a ready `GMAIL_ACCESS_TOKEN`. Alternative source:
Gmail IMAP (`imap.gmail.com` + app password) via the shared mail module.

Example: `{query: "in:inbox newer_than:7d", dest_mailbox: "INBOX",
job_name: "weekly-catchup", dry_run: true}` first, then re-run with
`dry_run: false` to copy.

## Tools

| Tool | What it does |
|---|---|
| `mail_search` | Search a mailbox: `mailbox`, `limit`, `unseen_only`, `seen`, `flagged`, `since`/`before` (`01-Jan-2025`), `from_`/`to`/`cc`/`subject`/`body`/`text`, `header`+`header_value`, `uids`, `larger`/`smaller` |
| `mail_read` | Full message: `mailbox`, `uid`, `mark_seen`, `include_body`, `max_bytes` → headers, `body_text`/`body_html`, attachments |
| `mail_send` | Send via SMTP: `to`, `subject`, `body_text`, `body_html`, `from_addr`, `cc`, `bcc`, `reply_to`, `in_reply_to`, `references`, `extra_headers`, `attachments` (`filename`, base64 `content`, `maintype`/`subtype`), `priority` |
| `mail_mailboxes` | List IMAP mailboxes |
| `mail_flag` / `mail_move` / `mail_delete` | Flag, move, delete messages |

## Run

```bash
cp mcp/.env.example mcp/.env   # fill in MAILU_IMAP_* / MAILU_SMTP_*
PYTHONPATH=mcp python3 -m mailu_mcp.server
```

Speak MCP over stdio: `initialize` → `tools/list` → `tools/call`.
See `feature/combined` for all lanes (mail + admin + Gmail import) at once.
