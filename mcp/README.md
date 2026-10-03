# Mailu MCP — `feature/admin-mcp` (stacked on `feature/mail-mcp`)

Mail tools plus the full Mailu admin REST API over MCP (stdio), stdlib only.

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
