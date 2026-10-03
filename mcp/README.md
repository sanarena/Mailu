# Mailu MCP — `feature/mail-mcp`

Mail tools for Mailu mailboxes over MCP (stdio), stdlib only.

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
