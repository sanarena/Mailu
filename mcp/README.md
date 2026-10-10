# Mailu MCP — `feature/http-auth`: everything over stdio or HTTP

Mail tools + full Mailu admin REST API + Gmail → Mailu copy jobs over MCP
(stdio or HTTP), Python 3.9+ stdlib only — no third-party dependencies.

## Branches

| Branch | Contains | Tools |
|---|---|---|
| `feature/mail-mcp` | IMAP search/read + SMTP send | 7 `mail_*` |
| `feature/admin-mcp` | + admin REST API (domains, users, aliases, relays, tokens, alternatives, managers, random-alias) | + 43 `admin_*` |
| `feature/gmail-import` | + Gmail search + copy jobs | + 3 `gmail_*` |
| `feature/combined` | all lanes + end-to-end smoke test | 53 total |
| `feature/http-auth` (here) | + HTTP transport with Bearer auth, k8s LB manifests | 53 over stdio or HTTP |

Branches are stacked (each builds on the previous) so every branch works
standalone; `feature/combined` is where all lanes are tested together.
Fork: `origin` = your fork, `upstream` = `Mailu/Mailu`.

## Admin tools (43)

Domains: `admin_domains_list`, `admin_domain_get/create/update/delete`,
`admin_domain_dkim` (rotate DKIM/DMARC keys), managers
(`admin_managers_list`, `admin_manager_add/check/remove`),
`admin_domain_users`, alternatives
(`admin_alternatives_list/get/create/delete`).
Users: `admin_users_list/get/create/update/delete` — every
`UserCreate`/`UserUpdate` parameter passes through (quota, forwarding,
autoreply, spam filter, …).
Aliases: `admin_aliases_list/get/create/update/delete`,
`admin_aliases_by_domain`, own anonymous aliases
(`admin_myaliases_list/delete/update`),
`admin_alias_domains_available`, `admin_random_alias_create`
(SimpleLogin/Bitwarden compatible; per-user `email`+`token` auth).
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

## HTTP (public load balancer)

Two roles — this is what keeps user A from ever seeing user B's mail:

- **Users** authenticate with HTTP Basic using their own Mailu login
  (`email:password` or `email:app-token`) and may call `mail_*` and
  `gmail_*` tools. Every request opens its IMAP/SMTP session with those
  exact credentials, so Dovecot/Postfix enforce the boundary — there is
  no shared mailbox identity in HTTP mode, and sends default to the
  caller's own address. Gmail import is per account too: pass your own
  `gmail_access_token` (or OAuth client/secret/refresh set) with each
  call; copies land in your mailbox and job state is namespaced per
  owner. Without per-call credentials, Gmail tools fail closed — users
  can never touch the server's Gmail account.
- **Operator** authenticates with `Bearer $MCP_HTTP_ADMIN_TOKEN` and may
  call everything: `admin_*` (global API) plus `mail_*`/`gmail_*`
  against the single server-configured mailbox (`MAILU_IMAP_*`) and
  Gmail account (or per-call credentials, which always win).

```bash
export MCP_HTTP_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
PYTHONPATH=mcp python3 -m mailu_mcp.server --http --port 8000
curl -s http://127.0.0.1:8000/healthz   # LB probe, no auth
curl -s -u you@example.com:secret \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:8000/mcp              # 7 mail_* + 3 gmail_* tools
curl -s -u you@example.com:secret \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"gmail_search","arguments":{"query":"in:inbox","gmail_access_token":"ya29..."}}}' \
  http://127.0.0.1:8000/mcp              # your Gmail, your credentials
curl -s -H "Authorization: Bearer $MCP_HTTP_ADMIN_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:8000/mcp              # all 53 tools
```

Rules: one JSON-RPC message per `POST /mcp` (same shapes as stdio);
`GET /healthz` is the only unauthenticated route; the server refuses to
start without `MCP_HTTP_ADMIN_TOKEN` (constant-time compare, 401/403);
failed logins are rate-limited per IP (429); bodies are capped at
`MCP_HTTP_MAX_BODY`. It speaks plain HTTP — always terminate TLS at the
load balancer / ingress.

Kubernetes: `mcp/k8s/deployment.yaml` (single replica, `/healthz`
liveness/readiness, state on the `mail-mcp-state` PVC) +
`mcp/k8s/service.yaml` (ClusterIP) + `mcp/k8s/ingress.yaml`
(`https://mcp.arminnikdel.com`, TLS at the ingress). Secrets come
from the live-only `mailu-creds` (`api-token`) and `mcp-admin-token`
(`token`) Secrets, never from git. These manifests mirror the Flux
repo `mailu/mcp-*.yaml`, which is the deploy source of truth.

## Verify (no live server needed)

```bash
python3 -m unittest discover -s mcp/tests   # 77 tests, must all pass
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | PYTHONPATH=mcp python3 -m mailu_mcp.server   # 7 mail + 43 admin + 3 gmail tools
```
